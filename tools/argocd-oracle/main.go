// Portions (localRepos) adapted from Argo CD, Copyright The Argo Authors,
// licensed under the Apache License, Version 2.0: see LICENSE-argo-cd and NOTICE.
//
// argocd-oracle expands ApplicationSets with Argo CD's own controller code, to
// produce the expected results of argocd-source-lint's ApplicationSet tests.
//
// It imports github.com/argoproj/argo-cd/v3 at the version pinned in go.mod and
// calls template.GenerateApplications with the List, Git, Matrix and Merge
// generators, wired as in generators.GetGenerators. The repo-server is replaced
// by localRepos, which reads a local Git checkout.
//
// It also renders single Go templates with utils.Render.Replace, the function
// the controller calls on every string of a goTemplate ApplicationSet, and
// lists the template functions that Argo CD's Go templates accept.
//
// Input (stdin): {"appsets": [...], "renders": [...]}. Each appset is
// {"appset": "<YAML>", "repo": "<checkout dir>", "newGitFileGlobbing": bool};
// each render is {"template": "...", "params": {...}, "options": [...]}.
// Output (stdout): {"appsets": [...], "renders": [...], "functions": [...]},
// in the input order. An appset result is {"applications": [...]} or
// {"error": "..."}; a render result is {"output": "..."} or {"error": "..."}.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/Masterminds/sprig/v3"
	"github.com/argoproj/argo-cd/v3/applicationset/controllers/template"
	"github.com/argoproj/argo-cd/v3/applicationset/generators"
	"github.com/argoproj/argo-cd/v3/applicationset/utils"
	argov1alpha1 "github.com/argoproj/argo-cd/v3/pkg/apis/application/v1alpha1"
	"github.com/argoproj/argo-cd/v3/util/git"
	log "github.com/sirupsen/logrus"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/yaml"
)

// kubeClient holds the `default` AppProject in `argocd`, which the Git
// generator reads to decide on commit signature checks (generators/git.go).
func kubeClient() client.Client {
	scheme := runtime.NewScheme()
	if err := argov1alpha1.AddToScheme(scheme); err != nil {
		panic(err)
	}
	project := &argov1alpha1.AppProject{ObjectMeta: metav1.ObjectMeta{Name: "default", Namespace: "argocd"}}
	return fake.NewClientBuilder().WithScheme(scheme).WithObjects(project).Build()
}

type input struct {
	AppSet             string `json:"appset"`
	Repo               string `json:"repo"`
	NewGitFileGlobbing bool   `json:"newGitFileGlobbing"`
}

type source struct {
	RepoURL        string `json:"repoURL"`
	Path           string `json:"path"`
	TargetRevision string `json:"targetRevision"`
	Chart          string `json:"chart"`
}

type application struct {
	Name                 string   `json:"name"`
	Namespace            string   `json:"namespace"`
	Project              string   `json:"project"`
	Sources              []source `json:"sources"`
	DestinationServer    string   `json:"destinationServer"`
	DestinationName      string   `json:"destinationName"`
	DestinationNamespace string   `json:"destinationNamespace"`
}

type output struct {
	Applications []application `json:"applications"`
	Error        string        `json:"error,omitempty"`
}

// localRepos answers the Git generator from a local checkout, whatever the
// repoURL and revision, as the repo-server would after checking it out.
type localRepos struct {
	root               string
	newGitFileGlobbing bool
}

// GetFiles follows Service.GetGitFiles in reposerver/repository/repository.go.
func (r localRepos) GetFiles(_ context.Context, repoURL, _, _, pattern string, _, _ bool) (map[string][]byte, error) {
	client, err := git.NewClientExt(repoURL, r.root, git.NopCreds{}, false, false, "", "")
	if err != nil {
		return nil, err
	}
	files, err := client.LsFiles(pattern, r.newGitFileGlobbing)
	if err != nil {
		return nil, err
	}
	res := make(map[string][]byte)
	for _, file := range files {
		content, err := os.ReadFile(filepath.Join(r.root, file))
		if err != nil {
			return nil, err
		}
		res[file] = content
	}
	return res, nil
}

// GetDirectories follows Service.GetGitDirectories in
// reposerver/repository/repository.go, with hidden directories skipped.
func (r localRepos) GetDirectories(_ context.Context, _, _, _ string, _, _ bool) ([]string, error) {
	var paths []string
	err := filepath.WalkDir(r.root, func(path string, entry fs.DirEntry, fnErr error) error {
		if fnErr != nil {
			return fnErr
		}
		if !entry.IsDir() {
			return nil
		}
		if strings.HasPrefix(entry.Name(), ".") {
			return filepath.SkipDir
		}
		rel, err := filepath.Rel(r.root, path)
		if err != nil {
			return err
		}
		if rel != "." {
			paths = append(paths, filepath.ToSlash(rel))
		}
		return nil
	})
	return paths, err
}

func expand(in input) output {
	var appset argov1alpha1.ApplicationSet
	// The API server receives the manifest as JSON converted by sigs.k8s.io/yaml.
	if err := yaml.Unmarshal([]byte(in.AppSet), &appset); err != nil {
		return output{Error: err.Error()}
	}
	repos := localRepos{root: in.Repo, newGitFileGlobbing: in.NewGitFileGlobbing}
	terminal := map[string]generators.Generator{
		"List": generators.NewListGenerator(),
		"Git":  generators.NewGitGenerator(repos, "argocd"),
	}
	nested := map[string]generators.Generator{
		"List":   terminal["List"],
		"Git":    terminal["Git"],
		"Matrix": generators.NewMatrixGenerator(terminal),
		"Merge":  generators.NewMergeGenerator(terminal),
	}
	top := map[string]generators.Generator{
		"List":   terminal["List"],
		"Git":    terminal["Git"],
		"Matrix": generators.NewMatrixGenerator(nested),
		"Merge":  generators.NewMergeGenerator(nested),
	}
	apps, _, err := template.GenerateApplications(log.NewEntry(log.StandardLogger()), appset, top, &utils.Render{}, kubeClient())
	if err != nil {
		// The controller stops here: no Application is created or updated.
		return output{Error: err.Error()}
	}
	res := output{Applications: []application{}}
	for _, app := range apps {
		a := application{
			Name:                 app.Name,
			Namespace:            app.Namespace,
			Project:              app.Spec.Project,
			DestinationServer:    app.Spec.Destination.Server,
			DestinationName:      app.Spec.Destination.Name,
			DestinationNamespace: app.Spec.Destination.Namespace,
			Sources:              []source{},
		}
		for _, s := range app.Spec.GetSources() {
			a.Sources = append(a.Sources, source{s.RepoURL, s.Path, s.TargetRevision, s.Chart})
		}
		res.Applications = append(res.Applications, a)
	}
	return res
}

type renderInput struct {
	Template string         `json:"template"`
	Params   map[string]any `json:"params"`
	Options  []string       `json:"options"`
}

type renderOutput struct {
	Output *string `json:"output,omitempty"`
	Error  string  `json:"error,omitempty"`
}

// render calls Replace as deeplyReplace does for each string of the template.
// An invalid option makes text/template panic; the panic is reported as an error.
func render(in renderInput) (res renderOutput) {
	defer func() {
		if r := recover(); r != nil {
			res = renderOutput{Error: fmt.Sprintf("panic: %v", r)}
		}
	}()
	out, err := (&utils.Render{}).Replace(in.Template, in.Params, true, in.Options)
	if err != nil {
		return renderOutput{Error: err.Error()}
	}
	return renderOutput{Output: &out}
}

// goBuiltins are text/template's predefined functions (builtins in
// text/template/funcs.go), which sprig.GenericFuncMap does not list.
var goBuiltins = []string{
	"and", "call", "eq", "ge", "gt", "html", "index", "js", "le", "len", "lt", "ne",
	"not", "or", "print", "printf", "println", "slice", "urlquery",
}

// functions lists the candidate names that parse in Argo CD's Go templates:
// sprig's, text/template's builtins, and the ones Argo CD adds or removes.
func functions() []string {
	candidates := map[string]bool{
		"env": true, "expandenv": true, "getHostByName": true,
		"normalize": true, "slugify": true, "toYaml": true, "fromYaml": true, "fromYamlArray": true,
	}
	for name := range sprig.GenericFuncMap() {
		candidates[name] = true
	}
	for _, name := range goBuiltins {
		candidates[name] = true
	}
	names := []string{}
	for name := range candidates {
		probe := "{{ if false }}{{ " + name + " }}{{ end }}"
		if _, err := (&utils.Render{}).Replace(probe, map[string]any{"x": ""}, true, nil); err == nil {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

func main() {
	log.SetOutput(os.Stderr)
	log.SetLevel(log.PanicLevel)
	var in struct {
		AppSets []input       `json:"appsets"`
		Renders []renderInput `json:"renders"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	out := struct {
		AppSets   []output       `json:"appsets"`
		Renders   []renderOutput `json:"renders"`
		Functions []string       `json:"functions"`
	}{AppSets: []output{}, Renders: []renderOutput{}, Functions: functions()}
	for _, appset := range in.AppSets {
		out.AppSets = append(out.AppSets, expand(appset))
	}
	for _, r := range in.Renders {
		out.Renders = append(out.Renders, render(r))
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}
