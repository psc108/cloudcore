package resources

import (
	"context"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
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

// objectsToList converts a slice of already-built attr.Value objects (all
// sharing objType) into a types.List, returning a null list when the slice
// is empty — same null-preserving convention as stringsToList/tagsToMap
// above, for an Optional (non-Computed) ListNestedAttribute. Found needed
// when InstanceResource.ImportState never set state.Users at all: the
// framework has no way to infer a nested object's element type from a bare
// empty Go slice, and errored ("MISSING TYPE" / DynamicPseudoType) trying —
// every ImportState for a real instance with no extra `users` configured
// hit this, which is exactly the common case (users is a rarely-used
// cloud-init extra, not the default image user).
func objectsToList(objType types.ObjectType, values []attr.Value) (types.List, diag.Diagnostics) {
	if len(values) == 0 {
		return types.ListNull(objType), nil
	}
	return types.ListValue(objType, values)
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
