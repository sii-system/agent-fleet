package dockerfile2llb

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/moby/buildkit/frontend/dockerui"
	"github.com/moby/buildkit/solver/pb"
	"github.com/stretchr/testify/require"
)

// Run the same test in a pristine and patched checkout, then compare snapshots.
// No protobuf graph is rewritten: decoded ExecOps are observation only.
func TestOpenSandboxCorpus(t *testing.T) {
	ctx := context.Background()
	data, err := os.ReadFile(os.Getenv("OPENSANDBOX_CORPUS"))
	require.NoError(t, err)
	var fixtures map[string]string
	require.NoError(t, json.Unmarshal(data, &fixtures))
	for name, fixture := range fixtures {
		t.Run(name, func(t *testing.T) {
			args := map[string]string{
				"OPENSANDBOX_APT_WRAPPER":       "wrapper-v1",
				"OPENSANDBOX_APT_REWRITER":      "rewriter-v1",
				"OPENSANDBOX_APT_SOURCE_MAP":    "map-v1",
				"OPENSANDBOX_FRONTEND_IDENTITY": "frontend-v1",
			}
			caps := pb.Caps.CapSet(pb.Caps.All())
			st, img, _, _, err := Dockerfile2LLB(ctx, []byte("FROM scratch AS fixture-base\n"+fixture), ConvertOpt{Config: dockerui.Config{BuildArgs: args}, LLBCaps: &caps})
			require.NoError(t, err)
			def, err := st.Marshal(ctx)
			require.NoError(t, err)
			var execs []*pb.ExecOp
			for _, dt := range def.Def {
				var op pb.Op
				require.NoError(t, op.Unmarshal(dt))
				if ex := op.GetExec(); ex != nil {
					execs = append(execs, ex)
				}
			}
			if name == "device" {
				require.Len(t, execs, 1)
				require.Len(t, execs[0].CdiDevices, 1)
				require.Equal(t, "example.com/device=probe", execs[0].CdiDevices[0].Name)
				require.True(t, execs[0].CdiDevices[0].Optional)
			}
			for _, env := range img.Config.Env {
				require.NotContains(t, env, "/run/opensandbox-apt")
			}
			if os.Getenv("OPENSANDBOX_PATCHED") == "1" {
				for _, ex := range execs {
					found := false
					for _, env := range ex.Meta.Env {
						found = found || strings.HasPrefix(env, "PATH=/run/opensandbox-apt/bin:")
					}
					require.True(t, found)
					count := 0
					for _, mount := range ex.Mounts {
						if strings.HasPrefix(mount.Dest, "/run/opensandbox-apt/") {
							count++
						}
					}
					require.Equal(t, 6, count)
				}
			}
			result, err := json.MarshalIndent(map[string]any{"execs": execs, "config": img.Config}, "", "  ")
			require.NoError(t, err)
			require.NoError(t, os.WriteFile(filepath.Join(os.Getenv("OPENSANDBOX_SNAPSHOTS"), name+".json"), result, 0644))
		})
	}
}
