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
// Input (stdin): a JSON array of {"appset": "<YAML>", "repo": "<checkout dir>",
// "newGitFileGlobbing": bool}. Output (stdout): a JSON array of
// {"applications": [...]} or {"error": "..."}, in the same order.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

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

func main() {
	log.SetOutput(os.Stderr)
	log.SetLevel(log.PanicLevel)
	var inputs []input
	if err := json.NewDecoder(os.Stdin).Decode(&inputs); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	outputs := make([]output, len(inputs))
	for i, in := range inputs {
		outputs[i] = expand(in)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(outputs); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}
