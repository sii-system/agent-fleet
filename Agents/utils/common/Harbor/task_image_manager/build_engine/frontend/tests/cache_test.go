package dockerfile2llb

import (
	"context"
	"testing"

	"github.com/moby/buildkit/client/llb"
	"github.com/moby/buildkit/solver/pb"
	"github.com/stretchr/testify/require"
)

func TestOpenSandboxCacheIdentity(t *testing.T) {
	values := map[string]string{
		"OPENSANDBOX_APT_WRAPPER":          "wrapper-v1",
		"OPENSANDBOX_APT_REWRITER":         "rewriter-v1",
		"OPENSANDBOX_APT_GATEWAY_ROOT":     "gateway-root-v1",
		"OPENSANDBOX_FRONTEND_IDENTITY":    "frontend-v1",
		"OPENSANDBOX_DOWNLOAD_WRAPPER":     "download-wrapper-v1",
		"OPENSANDBOX_DOWNLOAD_REWRITER":    "download-rewriter-v1",
		"OPENSANDBOX_DOWNLOAD_SOURCE":      "download-source-v1",
		"OPENSANDBOX_GITHUB_MIRROR_CONFIG": "gitconfig-v1",
		"OPENSANDBOX_CONDA_CONFIG":         "condarc-v1",
	}
	state := llb.Scratch().AddEnv("PATH", "/custom/bin").Dir("/work").User("1000:1000")
	marshal := func() []byte {
		opts, err := opensandboxRunOptions(state, values)
		require.NoError(t, err)
		out := state.Run(append([]llb.RunOption{llb.Args([]string{"apt-get", "update"})}, opts...)...).Root()
		path, _, err := out.GetEnv(context.Background(), "PATH")
		require.NoError(t, err)
		require.Equal(t, "/custom/bin", path)
		def, err := out.Marshal(context.Background())
		require.NoError(t, err)
		// The terminal op commits to input digests; metadata maps are not cache
		// identity and their protobuf iteration order is intentionally ignored.
		return def.Def[len(def.Def)-1]
	}
	original := marshal()
	require.Equal(t, original, marshal())
	for key, value := range values {
		values[key] = value + "-changed"
		require.NotEqual(t, original, marshal(), key)
		values[key] = value
	}
}

func TestOpenSandboxCondaMountIsTransient(t *testing.T) {
	require.Empty(t, opensandboxCondaOptions(map[string]string{}))
	state := llb.Scratch().AddEnv("PATH", "/custom/bin")
	opts := opensandboxCondaOptions(map[string]string{"OPENSANDBOX_CONDA_CONFIG": "condarc-v1"})
	result := state.Run(append([]llb.RunOption{llb.Args([]string{"conda", "install", "six"})}, opts...)...).Root()
	def, err := result.Marshal(context.Background())
	require.NoError(t, err)
	found := false
	for _, dt := range def.Def {
		var op pb.Op
		require.NoError(t, op.Unmarshal(dt))
		if exec := op.GetExec(); exec != nil {
			for _, mount := range exec.Mounts {
				if mount.Dest == "/etc/conda/condarc.d/99-agent-fleet-gateway.yaml" {
					found = true
					require.Equal(t, pb.MountType_SECRET, mount.MountType)
					require.Equal(t, "condarc-v1", mount.SecretOpt.ID)
					require.EqualValues(t, 0444, mount.SecretOpt.Mode)
				}
			}
		}
	}
	require.True(t, found)
	path, _, err := result.GetEnv(context.Background(), "PATH")
	require.NoError(t, err)
	require.Equal(t, "/custom/bin", path)
}

func TestOpenSandboxDownloadRuntimeIsAllOrNothing(t *testing.T) {
	complete := map[string]string{
		"OPENSANDBOX_DOWNLOAD_WRAPPER":  "download-wrapper-v1",
		"OPENSANDBOX_DOWNLOAD_REWRITER": "download-rewriter-v1",
		"OPENSANDBOX_DOWNLOAD_SOURCE":   "download-source-v1",
	}
	enabled, err := opensandboxDownloadEnabled(complete)
	require.NoError(t, err)
	require.True(t, enabled)

	for removed := range complete {
		partial := map[string]string{}
		for key, value := range complete {
			if key != removed {
				partial[key] = value
			}
		}
		_, err := opensandboxDownloadEnabled(partial)
		require.ErrorContains(t, err, "configured together", removed)
	}

	enabled, err = opensandboxDownloadEnabled(map[string]string{})
	require.NoError(t, err)
	require.False(t, enabled)
}
