# Why I wrote argocd-source-lint

I'm a data engineer. Part of my job is running the Kubernetes platform our data stack lives on, deployed by ArgoCD from a GitOps repo. What I was afraid of was simple: merging a change that breaks a service, or makes something disappear without anyone noticing.

So we wanted a CI check that would tell us, before merge, whether our ArgoCD files were actually doing what we thought. I looked for a linter that fit what we needed and didn't find one at the time. The tools I found either needed access to a cluster or checked something else: manifest schemas, policies, or the diff of what a change would render. So I started writing my own, out of curiosity as much as need.

The first version had four rules: files no Application deploys, broken Helm values references, operators missing their `ignoreDifferences`, and source paths that don't exist. About a week after the first commit, I published it on PyPI, mostly so our CI could install it with a plain `pip install`, and also because I suspected I wasn't the only one with these problems.

## The first run

The first time I ran it on our production repo, in September 2026, it exited with code 1 and listed 22 files that no Application deployed.

Ouch. I hadn't realized we had that many.

It wasn't wrong. Digging in, I found two patterns. The first was files sitting next to a `values.yaml`, like an `httproute.yaml` or a `namespace.yaml`. The Application used the values from the repo, but none of its sources covered those files, so our HTTP routes weren't managed by ArgoCD at all. The second was two whole components whose configuration had been sitting in Git for months, with no Application to deploy it. Mostly, I was relieved to find them before they caused an incident.

Earlier, while I was still writing the tool, it had already shown me that `directory.include` combined with `recurse` (which defaults to `false`) was hiding three files everyone, me included, assumed were deployed.

## Cleaning up

In October, one merge request removed 19 orphaned files: 562 lines, 15 of them YAML. Before the cleanup, about one YAML file in nine in our repo was never deployed. Three Applications got an extra source, so ArgoCD finally manages their routes.

And then those routes went permanently OutOfSync: now that ArgoCD managed them, it was comparing them to the API's default values. We fixed it by writing those defaults into Git. Oddly, that was the reassuring part. If fixing the wiring exposes a new difference, the fix is real.

## Where it is now

Since late October it runs as a blocking job in our GitLab CI, next to yamllint and gitleaks. It has blocked one merge request so far: a new component whose five YAML files had no Application to deploy them. Merged as it was, it would never have reached the cluster, and nothing would have said so. The next commit in the same merge request added the Application.

As far as I know, our repo is the only place it runs. It has been tested on very few real repos, so I expect it to be wrong in ways I haven't seen yet.

It has already been wrong. Early on, when a chart came from another repo and its values through `$ref`, it reported the values file as never deployed; I fixed that before 1.0. More recently, it flagged files on my machine that weren't even tracked by Git; since 1.4.1 it only looks at tracked files. The ones I know about and haven't fixed yet are listed in the [README](https://github.com/gillesl-dev/argocd-source-lint#limitations).

It also doesn't try to do everything: it doesn't render Helm or Kustomize, it looks at one repo at a time, and it doesn't expand every ApplicationSet generator. If you need a diff of what will change in your cluster, argocd-diff-preview and kubechecks do that. Other ArgoCD linters exist too; the README [compares them](https://github.com/gillesl-dev/argocd-source-lint#compared-to-other-tools).

## How I built it

I built it with an AI coding assistant (Claude Code). I'm saying it upfront because you'd find out anyway, and because I'd rather you judge the result: the rules follow ArgoCD's source code, which the comments cite wherever the behavior isn't obvious, and there are more than 500 tests running on Linux and Windows.

## If you try it

I'm sharing it to get feedback: I'd really like to know how it behaves on a repo that isn't ours, good or bad. Especially if it's wrong.

- A false positive: please [open an issue](https://github.com/gillesl-dev/argocd-source-lint/issues), even a rough one.
- An operator that `missing-ignore-diff` should know about, or an ApplicationSet generator you need: same place.
- Anything else: [Discussions](https://github.com/gillesl-dev/argocd-source-lint/discussions).

To try it: `pipx install argocd-source-lint`, list your manifest folders under `scan_roots` in a `.argocd-lint.yaml` (see the [quick start](https://github.com/gillesl-dev/argocd-source-lint#quick-start)), then run `argocd-source-lint .` at the root of your GitOps repo. On an existing repo, `--write-baseline` accepts what's already there, so you only see new problems.

Thanks for reading.

— Gilles
