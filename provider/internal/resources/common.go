package resources

import (
	"context"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// tagsToMap converts a map[string]string from the API into a types.Map.
// Returns a null map when the API returns nil or empty, preserving the
// Optional attribute's null state when no tags were configured.
func tagsToMap(ctx context.Context, tags map[string]string) (types.Map, diag.Diagnostics) {
	if len(tags) == 0 {
		return types.MapNull(types.StringType), nil
	}
	return types.MapValueFrom(ctx, types.StringType, tags)
}

// stringsToList converts a []string from the API into a types.List.
// Returns a null list when the slice is nil or empty, preserving the
// Optional attribute's null state when nothing was configured.
func stringsToList(ctx context.Context, ss []string) (types.List, diag.Diagnostics) {
	if len(ss) == 0 {
		return types.ListNull(types.StringType), nil
	}
	return types.ListValueFrom(ctx, types.StringType, ss)
}

// splitTwo splits s on the first "/" into exactly two parts.
// Returns nil when s contains no "/" separator.
// Used by ImportState implementations that accept "parent_id/child_id" format.
func splitTwo(s string) []string {
	idx := strings.IndexByte(s, '/')
	if idx < 0 {
		return nil
	}
	return []string{s[:idx], s[idx+1:]}
}
