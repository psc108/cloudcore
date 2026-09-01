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

var _ resource.Resource = &VPCResource{}
var _ resource.ResourceWithImportState = &VPCResource{}

type VPCResource struct {
	client *client.Client
}

type VPCResourceModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	CIDRBlock   types.String `tfsdk:"cidr_block"`
	DNSSupport  types.Bool   `tfsdk:"dns_support"`
	Status      types.String `tfsdk:"status"`
	CreatedAt   types.String `tfsdk:"created_at"`
	Tags        types.Map    `tfsdk:"tags"`
}

type vpcAPIModel struct {
	ID         string            `json:"id"`
	Name       string            `json:"name"`
	CIDRBlock  string            `json:"cidr_block"`
	DNSSupport bool              `json:"dns_support"`
	Status     string            `json:"status"`
	CreatedAt  string            `json:"created_at"`
	Tags       map[string]string `json:"tags"`
}

func NewVPCResource() resource.Resource { return &VPCResource{} }

func (r *VPCResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_vpc"
}

func (r *VPCResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore VPC (isolated virtual network). API path: `/v1/vpcs`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:            true,
				Description:         "API-assigned VPC identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name":       schema.StringAttribute{Required: true, Description: "Human-readable VPC name."},
			"cidr_block": schema.StringAttribute{Required: true, Description: "IPv4 CIDR block for the VPC (e.g. '10.0.0.0/16')."},
			"dns_support": schema.BoolAttribute{
				Optional:    true,
				Computed:    true,
				Description: "Whether DNS resolution is enabled in the VPC. Defaults to true.",
			},
			"status": schema.StringAttribute{
				Computed:    true,
				Description: "Current VPC status (API-assigned).",
			},
			"created_at": schema.StringAttribute{
				Computed:    true,
				Description: "ISO 8601 timestamp when the VPC was created (API-assigned).",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"tags": schema.MapAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "Key/value tags to attach to the VPC.",
			},
		},
	}
}

func (r *VPCResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *VPCResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan VPCResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := vpcAPIModel{
		Name:       plan.Name.ValueString(),
		CIDRBlock:  plan.CIDRBlock.ValueString(),
		DNSSupport: plan.DNSSupport.ValueBool(),
		Tags:       tags,
	}

	var result vpcAPIModel
	if err := r.client.Post(ctx, "/v1/vpcs", body, &result); err != nil {
		resp.Diagnostics.AddError("Create VPC failed", err.Error())
		return
	}

	plan.ID = types.StringValue(result.ID)
	plan.DNSSupport = types.BoolValue(result.DNSSupport)
	plan.Status = types.StringValue(result.Status)
	plan.CreatedAt = types.StringValue(result.CreatedAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *VPCResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state VPCResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result vpcAPIModel
	if err := r.client.Get(ctx, "/v1/vpcs/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read VPC failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.CIDRBlock = types.StringValue(result.CIDRBlock)
	state.DNSSupport = types.BoolValue(result.DNSSupport)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)
	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *VPCResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan VPCResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := vpcAPIModel{
		Name:       plan.Name.ValueString(),
		CIDRBlock:  plan.CIDRBlock.ValueString(),
		DNSSupport: plan.DNSSupport.ValueBool(),
		Tags:       tags,
	}

	var result vpcAPIModel
	if err := r.client.Put(ctx, "/v1/vpcs/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update VPC failed", err.Error())
		return
	}
	plan.DNSSupport = types.BoolValue(result.DNSSupport)
	plan.Status = types.StringValue(result.Status)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *VPCResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state VPCResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/vpcs/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete VPC failed", err.Error())
	}
}

func (r *VPCResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var state VPCResourceModel
	state.ID = types.StringValue(req.ID)

	var result vpcAPIModel
	if err := r.client.Get(ctx, "/v1/vpcs/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import VPC failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.CIDRBlock = types.StringValue(result.CIDRBlock)
	state.DNSSupport = types.BoolValue(result.DNSSupport)
	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
