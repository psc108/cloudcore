package resources

import (
	"context"
	"fmt"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &DNSZoneResource{}
var _ resource.ResourceWithImportState = &DNSZoneResource{}

type DNSZoneResource struct {
	client *client.Client
}

type DNSZoneResourceModel struct {
	ID        types.String `tfsdk:"id"`
	Name      types.String `tfsdk:"name"`
	CreatedAt types.String `tfsdk:"created_at"`
}

type dnsZoneAPIModel struct {
	Name      string `json:"name"`
	CreatedAt string `json:"created_at"`
}

type dnsZoneListAPIModel struct {
	Items []dnsZoneAPIModel `json:"items"`
}

func NewDNSZoneResource() resource.Resource { return &DNSZoneResource{} }

func (r *DNSZoneResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_dns_zone"
}

func (r *DNSZoneResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore DNS zone. The zone `name` is used as the resource ID. API path: `/v1/dns/zones`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "Zone identifier (same as name).",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Required:    true,
				Description: "Zone name (e.g. 'myapp.cloudcore.local'). Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"created_at": schema.StringAttribute{
				Computed:    true,
				Description: "Timestamp when the zone was created.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
		},
	}
}

func (r *DNSZoneResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("got %T", req.ProviderData))
		return
	}
	r.client = c
}

func (r *DNSZoneResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan DNSZoneResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result dnsZoneAPIModel
	if err := r.client.Post(ctx, "/v1/dns/zones", map[string]string{"name": plan.Name.ValueString()}, &result); err != nil {
		resp.Diagnostics.AddError("Create DNS zone failed", err.Error())
		return
	}

	plan.ID = plan.Name
	plan.CreatedAt = types.StringValue(result.CreatedAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *DNSZoneResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state DNSZoneResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result dnsZoneListAPIModel
	if err := r.client.Get(ctx, "/v1/dns/zones", &result); err != nil {
		resp.Diagnostics.AddError("Read DNS zones failed", err.Error())
		return
	}
	for _, z := range result.Items {
		if z.Name == state.Name.ValueString() {
			state.ID = state.Name
			state.CreatedAt = types.StringValue(z.CreatedAt)
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *DNSZoneResource) Update(_ context.Context, _ resource.UpdateRequest, _ *resource.UpdateResponse) {
	// DNS zones have no mutable fields — name change forces replacement.
}

func (r *DNSZoneResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state DNSZoneResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/dns/zones/"+state.Name.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete DNS zone failed", err.Error())
	}
}

func (r *DNSZoneResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var result dnsZoneListAPIModel
	if err := r.client.Get(ctx, "/v1/dns/zones", &result); err != nil {
		resp.Diagnostics.AddError("Import DNS zone failed", err.Error())
		return
	}
	for _, z := range result.Items {
		if z.Name == req.ID {
			state := DNSZoneResourceModel{
				ID:        types.StringValue(z.Name),
				Name:      types.StringValue(z.Name),
				CreatedAt: types.StringValue(z.CreatedAt),
			}
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.Diagnostics.AddError("Import DNS zone failed", fmt.Sprintf("zone '%s' not found", req.ID))
}
