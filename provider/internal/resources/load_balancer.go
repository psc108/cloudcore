package resources

import (
	"context"
	"errors"
	"fmt"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &LoadBalancerResource{}
var _ resource.ResourceWithImportState = &LoadBalancerResource{}

type LoadBalancerResource struct {
	client *client.Client
}

type LoadBalancerResourceModel struct {
	ID        types.String `tfsdk:"id"`
	Name      types.String `tfsdk:"name"`
	Type      types.String `tfsdk:"type"`
	VPCID     types.String `tfsdk:"vpc_id"`
	SubnetIDs types.List   `tfsdk:"subnet_ids"`
	Internal  types.Bool   `tfsdk:"internal"`
	DNSName   types.String `tfsdk:"dns_name"`
	Status    types.String `tfsdk:"status"`
	CreatedAt types.String `tfsdk:"created_at"`
	Tags      types.Map    `tfsdk:"tags"`
}

type lbAPIModel struct {
	ID        string            `json:"id"`
	Name      string            `json:"name"`
	Type      string            `json:"type"`
	VPCID     string            `json:"vpc_id"`
	SubnetIDs []string          `json:"subnet_ids"`
	Internal  bool              `json:"internal"`
	DNSName   string            `json:"dns_name"`
	Status    string            `json:"status"`
	CreatedAt string            `json:"created_at"`
	Tags      map[string]string `json:"tags"`
}

func NewLoadBalancerResource() resource.Resource { return &LoadBalancerResource{} }

func (r *LoadBalancerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_load_balancer"
}

func (r *LoadBalancerResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{Required: true},
			"type": schema.StringAttribute{
				Required:    true,
				Description: "Load balancer type: 'network' (L4) or 'application' (L7).",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"vpc_id": schema.StringAttribute{Required: true},
			"subnet_ids": schema.ListAttribute{
				Required:    true,
				ElementType: types.StringType,
			},
			"internal": schema.BoolAttribute{
				Optional: true,
				Computed: true,
			},
			"dns_name":   schema.StringAttribute{Computed: true},
			"status":     schema.StringAttribute{Computed: true},
			"created_at": schema.StringAttribute{
				Computed: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"tags": schema.MapAttribute{
				Optional:    true,
				ElementType: types.StringType,
			},
		},
	}
}

func (r *LoadBalancerResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *LoadBalancerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan LoadBalancerResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	subnetIDs := []string{}
	resp.Diagnostics.Append(plan.SubnetIDs.ElementsAs(ctx, &subnetIDs, false)...)
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := lbAPIModel{
		Name:      plan.Name.ValueString(),
		Type:      plan.Type.ValueString(),
		VPCID:     plan.VPCID.ValueString(),
		SubnetIDs: subnetIDs,
		Internal:  plan.Internal.ValueBool(),
		Tags:      tags,
	}

	var result lbAPIModel
	if err := r.client.Post(ctx, "/v1/load-balancers", body, &result); err != nil {
		resp.Diagnostics.AddError("Create load balancer failed", err.Error())
		return
	}

	plan.ID = types.StringValue(result.ID)
	plan.DNSName = types.StringValue(result.DNSName)
	plan.Internal = types.BoolValue(result.Internal)
	plan.Status = types.StringValue(result.Status)
	plan.CreatedAt = types.StringValue(result.CreatedAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *LoadBalancerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state LoadBalancerResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result lbAPIModel
	if err := r.client.Get(ctx, "/v1/load-balancers/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read load balancer failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.Type = types.StringValue(result.Type)
	state.VPCID = types.StringValue(result.VPCID)
	state.Internal = types.BoolValue(result.Internal)
	state.DNSName = types.StringValue(result.DNSName)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)

	subnetIDs, diags := types.ListValueFrom(ctx, types.StringType, result.SubnetIDs)
	resp.Diagnostics.Append(diags...)
	state.SubnetIDs = subnetIDs

	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags

	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *LoadBalancerResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan LoadBalancerResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	subnetIDs := []string{}
	resp.Diagnostics.Append(plan.SubnetIDs.ElementsAs(ctx, &subnetIDs, false)...)
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := lbAPIModel{
		Name:      plan.Name.ValueString(),
		Type:      plan.Type.ValueString(),
		VPCID:     plan.VPCID.ValueString(),
		SubnetIDs: subnetIDs,
		Internal:  plan.Internal.ValueBool(),
		Tags:      tags,
	}

	var result lbAPIModel
	if err := r.client.Put(ctx, "/v1/load-balancers/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update load balancer failed", err.Error())
		return
	}
	plan.DNSName = types.StringValue(result.DNSName)
	plan.Internal = types.BoolValue(result.Internal)
	plan.Status = types.StringValue(result.Status)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *LoadBalancerResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state LoadBalancerResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/load-balancers/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete load balancer failed", err.Error())
	}
}

func (r *LoadBalancerResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var state LoadBalancerResourceModel
	state.ID = types.StringValue(req.ID)

	var result lbAPIModel
	if err := r.client.Get(ctx, "/v1/load-balancers/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import load balancer failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.Type = types.StringValue(result.Type)
	state.VPCID = types.StringValue(result.VPCID)
	state.Internal = types.BoolValue(result.Internal)
	state.DNSName = types.StringValue(result.DNSName)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)

	subnetIDs, diags := types.ListValueFrom(ctx, types.StringType, result.SubnetIDs)
	resp.Diagnostics.Append(diags...)
	state.SubnetIDs = subnetIDs

	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags

	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
