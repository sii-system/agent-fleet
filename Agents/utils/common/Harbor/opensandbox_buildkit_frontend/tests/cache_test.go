package dockerfile2llb

import (
	"context"
	"testing"

	"github.com/moby/buildkit/client/llb"
	"github.com/stretchr/testify/require"
)

func TestOpenSandboxCacheIdentity(t *testing.T) {
	values := map[string]string{
		"OPENSANDBOX_APT_WRAPPER":       "wrapper-v1",
		"OPENSANDBOX_APT_REWRITER":      "rewriter-v1",
		"OPENSANDBOX_APT_SOURCE_MAP":    "map-v1",
		"OPENSANDBOX_FRONTEND_IDENTITY": "frontend-v1",
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
