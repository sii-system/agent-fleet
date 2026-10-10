package dockerfile2llb

import (
	"context"
	"fmt"

	"github.com/moby/buildkit/client/llb"
)

const (
	opensandboxRoot         = "/run/opensandbox-apt"
	opensandboxDownloadRoot = "/run/opensandbox-download"
)

// opensandboxPathOption shadows command lookup only for this RUN. APT is
// always enabled by the image manager, so its directory is always first in the
// base instrumented PATH. When the optional download source is configured, the
// curl/wget directory precedes APT as an independent namespace. The image's
// authored PATH follows both entries unchanged, preserving lookup for every
// other command in the current build stage.
func opensandboxPathOption(state llb.State, downloadEnabled bool) (llb.RunOption, error) {
	path, _, err := state.GetEnv(context.TODO(), "PATH")
	if err != nil {
		return nil, err
	}
	instrumentedPath := opensandboxRoot + "/bin"
	if downloadEnabled {
		instrumentedPath = opensandboxDownloadRoot + "/bin:" + instrumentedPath
	}
	if path != "" {
		instrumentedPath += ":" + path
	}
	return llb.AddEnv("PATH", instrumentedPath), nil
}

// opensandboxAptOptions installs the same content-addressed shell wrapper under
// the apt and apt-get names, plus its awk source rewriter and Gateway root.
// The wrapper sees the task-authored /etc/apt files through the normal
// root filesystem, creates an invocation-local view under the tmpfs added by
// opensandboxIdentityOptions, and then invokes /usr/bin/apt or apt-get. All four
// secret IDs (producing four mounts) are required even for tasks that never call
// APT so every RUN has a
// mechanically identical and cache-safe instrumentation contract.
func opensandboxAptOptions(values map[string]string) ([]llb.RunOption, error) {
	options := []llb.RunOption{}
	for _, asset := range []struct {
		key, target string
		mode        int
	}{
		{"OPENSANDBOX_APT_WRAPPER", "/bin/apt", 0555},
		{"OPENSANDBOX_APT_WRAPPER", "/bin/apt-get", 0555},
		{"OPENSANDBOX_APT_REWRITER", "/source-rewriter.awk", 0444},
		{"OPENSANDBOX_APT_GATEWAY_ROOT", "/gateway-root", 0444},
	} {
		id := values[asset.key]
		if id == "" {
			return nil, fmt.Errorf("Harbor task image frontend requires build arg %s", asset.key)
		}
		options = append(options, llb.AddSecret(opensandboxRoot+asset.target,
			llb.SecretID(id), llb.SecretFileOpt(0, 0, asset.mode)))
	}
	return options, nil
}

// opensandboxDownloadEnabled validates the all-or-nothing optional download
// contract. A partial configuration must fail while lowering the Dockerfile;
// silently omitting one mount could either break every curl/wget invocation or,
// worse, allow the real tool to contact an upstream origin directly.
func opensandboxDownloadEnabled(values map[string]string) (bool, error) {
	keys := []string{
		"OPENSANDBOX_DOWNLOAD_WRAPPER",
		"OPENSANDBOX_DOWNLOAD_REWRITER",
		"OPENSANDBOX_DOWNLOAD_SOURCE",
	}
	configured := 0
	for _, key := range keys {
		if values[key] != "" {
			configured++
		}
	}
	if configured != 0 && configured != len(keys) {
		return false, fmt.Errorf("Harbor task image download runtime build args must be configured together")
	}
	return configured == len(keys), nil
}

// opensandboxDownloadOptions places one POSIX shell wrapper under both command
// names. The wrapper runs after shell expansion, so it sees URLs assembled from
// ARG/ENV values and URLs inside dynamically generated scripts. URL routing is
// delegated to a small read-only awk helper; the provider-neutral source root
// is a separate secret. Mounting these inputs instead of embedding them in the
// Dockerfile keeps source configuration and helper code out of image layers.
func opensandboxDownloadOptions(values map[string]string) []llb.RunOption {
	wrapper := values["OPENSANDBOX_DOWNLOAD_WRAPPER"]
	options := make([]llb.RunOption, 0, 4)
	for _, target := range []string{"/bin/curl", "/bin/wget"} {
		options = append(options, llb.AddSecret(opensandboxDownloadRoot+target,
			llb.SecretID(wrapper), llb.SecretFileOpt(0, 0, 0555)))
	}
	options = append(options,
		llb.AddSecret(opensandboxDownloadRoot+"/url-rewriter.awk",
			llb.SecretID(values["OPENSANDBOX_DOWNLOAD_REWRITER"]), llb.SecretFileOpt(0, 0, 0444)),
		llb.AddSecret(opensandboxDownloadRoot+"/source",
			llb.SecretID(values["OPENSANDBOX_DOWNLOAD_SOURCE"]), llb.SecretFileOpt(0, 0, 0444)),
	)
	return options
}

// opensandboxGitMirrorOptions shadows /etc/gitconfig only when the manager has
// produced a mirror config. Applying this at ExecOp level covers Git calls in
// dynamic scripts and removes the previous need to add --mount syntax to every
// RUN instruction. An empty value deliberately means no mount and preserves the
// stage's own /etc/gitconfig.
func opensandboxGitMirrorOptions(values map[string]string) []llb.RunOption {
	if gitConfig := values["OPENSANDBOX_GITHUB_MIRROR_CONFIG"]; gitConfig != "" {
		return []llb.RunOption{llb.AddSecret("/etc/gitconfig",
			llb.SecretID(gitConfig), llb.SecretFileOpt(0, 0, 0444))}
	}
	return nil
}

// Normal conda/mamba rc discovery reads this system config only during RUN.
// It maps defaults and conda-forge without overriding channel order, priority,
// user configs, explicit rc-file/no-rc options, or unrelated named channels.
func opensandboxCondaOptions(values map[string]string) []llb.RunOption {
	if config := values["OPENSANDBOX_CONDA_CONFIG"]; config != "" {
		return []llb.RunOption{llb.AddSecret("/etc/conda/condarc.d/99-agent-fleet-gateway.yaml",
			llb.SecretID(config), llb.SecretFileOpt(0, 0, 0444))}
	}
	return nil
}

// opensandboxIdentityOptions adds two pieces shared by the runtime adapters.
// The identity secret ID is derived from the frontend digest. BuildKit does not
// hash secret contents into ExecOp cache keys, so content-addressed secret IDs
// are the explicit cache boundary for helper changes. The tmpfs is writable
// scratch space used by the APT wrapper; because it is a RUN mount, none of its
// reconciliation files can be committed to the output snapshot.
func opensandboxIdentityOptions(values map[string]string) ([]llb.RunOption, error) {
	// Secret contents do not affect solver cache keys. A content-addressed ID
	// commits to the frontend implementation as well as the runtime asset IDs.
	identity := values["OPENSANDBOX_FRONTEND_IDENTITY"]
	if identity == "" {
		return nil, fmt.Errorf("Harbor task image frontend requires OPENSANDBOX_FRONTEND_IDENTITY")
	}
	return []llb.RunOption{
		llb.AddSecret(opensandboxRoot+"/frontend-identity", llb.SecretID(identity), llb.SecretFileOpt(0, 0, 0444)),
		llb.AddMount(opensandboxRoot+"/shadow", llb.Scratch(), llb.Tmpfs()),
	}, nil
}

// opensandboxRunOptions is the only entry point called by the upstream
// dockerfile2llb patch. It composes independent pieces of build-time
// instrumentation after upstream BuildKit has parsed the complete RUN
// instruction (shell form, JSON form, heredocs, mounts, network/security flags,
// and devices) and immediately before State.Run creates the ExecOp.
//
// Every injection helper above returns llb.RunOption values. These options
// affect only the current ExecOp: none of them mutate d.state or the image
// config. Consequently the temporary PATH entries, secret files, and tmpfs
// mount are absent from the committed root filesystem and from ENV inherited
// by later stages. Keeping each injected concern in a separate helper makes
// that non-persistence boundary reviewable without inspecting command text or
// rewriting protobufs.
func opensandboxRunOptions(state llb.State, values map[string]string) ([]llb.RunOption, error) {
	downloadEnabled, err := opensandboxDownloadEnabled(values)
	if err != nil {
		return nil, err
	}

	pathOption, err := opensandboxPathOption(state, downloadEnabled)
	if err != nil {
		return nil, err
	}
	options := []llb.RunOption{pathOption}

	aptOptions, err := opensandboxAptOptions(values)
	if err != nil {
		return nil, err
	}
	options = append(options, aptOptions...)

	if downloadEnabled {
		options = append(options, opensandboxDownloadOptions(values)...)
	}
	options = append(options, opensandboxGitMirrorOptions(values)...)
	options = append(options, opensandboxCondaOptions(values)...)

	identityOptions, err := opensandboxIdentityOptions(values)
	if err != nil {
		return nil, err
	}
	return append(options, identityOptions...), nil
}
