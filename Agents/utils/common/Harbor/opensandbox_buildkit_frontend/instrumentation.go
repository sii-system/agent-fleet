package dockerfile2llb

import (
	"context"
	"fmt"

	"github.com/moby/buildkit/client/llb"
)

const opensandboxRoot = "/run/opensandbox-apt"

// These options affect the ExecOp only. Mutating d.state would leak PATH into
// subsequent ENV expansion and inherited stages, even without changing config.
func opensandboxRunOptions(state llb.State, values map[string]string) ([]llb.RunOption, error) {
	path, _, err := state.GetEnv(context.TODO(), "PATH")
	if err != nil {
		return nil, err
	}
	opts := []llb.RunOption{llb.AddEnv("PATH", opensandboxRoot+"/bin:"+path)}
	for _, asset := range []struct {
		key, target string
		mode        int
	}{
		{"OPENSANDBOX_APT_WRAPPER", "/bin/apt", 0555},
		{"OPENSANDBOX_APT_WRAPPER", "/bin/apt-get", 0555},
		{"OPENSANDBOX_APT_REWRITER", "/source-rewriter.awk", 0444},
		{"OPENSANDBOX_APT_SOURCE_MAP", "/source-map", 0444},
	} {
		id := values[asset.key]
		if id == "" {
			return nil, fmt.Errorf("OpenSandbox frontend requires build arg %s", asset.key)
		}
		opts = append(opts, llb.AddSecret(opensandboxRoot+asset.target,
			llb.SecretID(id), llb.SecretFileOpt(0, 0, asset.mode)))
	}
	// Secret contents do not affect solver cache keys. A content-addressed ID
	// commits to the frontend implementation as well as the runtime asset IDs.
	identity := values["OPENSANDBOX_FRONTEND_IDENTITY"]
	if identity == "" {
		return nil, fmt.Errorf("OpenSandbox frontend requires OPENSANDBOX_FRONTEND_IDENTITY")
	}
	opts = append(opts,
		llb.AddSecret(opensandboxRoot+"/frontend-identity", llb.SecretID(identity), llb.SecretFileOpt(0, 0, 0444)),
		llb.AddMount(opensandboxRoot+"/shadow", llb.Scratch(), llb.Tmpfs()),
	)
	return opts, nil
}
