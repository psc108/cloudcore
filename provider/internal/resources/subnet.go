package resources

import (
	"context"
	"errors"
	"fmt"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &SubnetResource{}
var _ resource.ResourceWithImportState = &SubnetResource{}

type SubnetResource struct {
	client *client.Client
}

type SubnetResourceModel struct {
	ID        types.String `tfsdk:"id"`
	Name      types.String `tfsdk:"name"`
	VPCID     types.String `tfsdk:"vpc_id"`
	CIDRBlock types.String `tfsdk:"cidr_block"`
	Public    types.Bool   `tfsdk:"public"`
	Zone      types.String `tfsdk:"zone"`
	Status    types.String `tfsdk:"status"`
	CreatedAt types.String `tfsdk:"created_at"`
	Tags      types.Map    `tfsdk:"tags"`
}

type subnetAPIModel struct {
	ID        string            `json:"id"`
	Name      string            `json:"name"`
	VPCID     string            `json:"vpc_id"`
	CIDRBlock string            `json:"cidr_block"`
	Public    bool              `json:"public"`
	Zone      string            `json:"zone"`
	Status    string            `json:"status"`
	CreatedAt string            `json:"created_at"`
	Tags      map[string]string `json:"tags"`
}

func NewSubnetResource() resource.Resource { return &SubnetResource{} }

func (r *SubnetResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_subnet"
}

func (r *SubnetResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore subnet within a VPC. API path: `/v1/subnets`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "API-assigned subnet identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{Required: true, Description: "Subnet name."},
			"vpc_id": schema.StringAttribute{
				Required:    true,
				Description: "VPC this subnet belongs to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"cidr_block": schema.StringAttribute{
				Required:    true,
				Description: "IPv4 CIDR block for the subnet. Must be contained within the VPC CIDR. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"public": schema.BoolAttribute{
				Optional:    true,
				Computed:    true,
				Default:     booldefault.StaticBool(false),
				Description: "Whether this is a public-facing subnet. Informational — attach an internet gateway and route table to make it routable.",
			},
			"zone": schema.StringAttribute{
				Optional:    true,
				Computed:    true,
				Default:     stringdefault.StaticString("a"),
				Description: "Logical availability zone label (e.g. 'a', 'b'). CloudCore has no physical AZ concept.",
			},
			"status": schema.StringAttribute{Computed: true, Description: "Current subnet status (API-assigned)."},
			"created_at": schema.StringAttribute{
				Computed:    true,
				Description: "ISO 8601 timestamp when the subnet was created (API-assigned).",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"tags": schema.MapAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "Key/value tags to attach to the subnet.",
			},
		},
	}
}

func (r *SubnetResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func subnetMapToState(ctx context.Context, result subnetAPIModel, state *SubnetResourceModel) error {
	state.ID = types.StringValue(result.ID)
	state.Name = types.StringValue(result.Name)
	state.VPCID = types.StringValue(result.VPCID)
	state.CIDRBlock = types.StringValue(result.CIDRBlock)
	state.Public = types.BoolValue(result.Public)
	state.Zone = types.StringValue(result.Zone)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)
	tags, diags := tagsToMap(ctx, result.Tags)
	if diags.HasError() {
		return fmt.Errorf("converting tags")
	}
	state.Tags = tags
	return nil
}

func (r *SubnetResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan SubnetResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := subnetAPIModel{
		Name:      plan.Name.ValueString(),
		VPCID:     plan.VPCID.ValueString(),
		CIDRBlock: plan.CIDRBlock.ValueString(),
		Public:    plan.Public.ValueBool(),
		Zone:      plan.Zone.ValueString(),
		Tags:      tags,
	}

	var result subnetAPIModel
	if err := r.client.Post(ctx, "/v1/subnets", body, &result); err != nil {
		resp.Diagnostics.AddError("Create subnet failed", err.Error())
		return
	}
	if err := subnetMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map subnet state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *SubnetResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state SubnetResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result subnetAPIModel
	if err := r.client.Get(ctx, "/v1/subnets/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read subnet failed", err.Error())
		return
	}
	if err := subnetMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map subnet state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *SubnetResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan SubnetResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := subnetAPIModel{
		Name:   plan.Name.ValueString(),
		Public: plan.Public.ValueBool(),
		Zone:   plan.Zone.ValueString(),
		Tags:   tags,
	}

	var result subnetAPIModel
	if err := r.client.Put(ctx, "/v1/subnets/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update subnet failed", err.Error())
		return
	}
	if err := subnetMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map subnet state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *SubnetResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state SubnetResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/subnets/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete subnet failed", err.Error())
	}
}

func (r *SubnetResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var result subnetAPIModel
	if err := r.client.Get(ctx, "/v1/subnets/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import subnet failed", err.Error())
		return
	}
	var state SubnetResourceModel
	if err := subnetMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map subnet state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
