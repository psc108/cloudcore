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

var _ resource.Resource = &InternetGatewayResource{}
var _ resource.ResourceWithImportState = &InternetGatewayResource{}

type InternetGatewayResource struct {
	client *client.Client
}

type InternetGatewayResourceModel struct {
	ID        types.String `tfsdk:"id"`
	Name      types.String `tfsdk:"name"`
	VPCID     types.String `tfsdk:"vpc_id"`
	Status    types.String `tfsdk:"status"`
	CreatedAt types.String `tfsdk:"created_at"`
	Tags      types.Map    `tfsdk:"tags"`
}

type igwAPIModel struct {
	ID        string            `json:"id"`
	Name      string            `json:"name"`
	VPCID     string            `json:"vpc_id"`
	Status    string            `json:"status"`
	CreatedAt string            `json:"created_at"`
	Tags      map[string]string `json:"tags"`
}

func NewInternetGatewayResource() resource.Resource { return &InternetGatewayResource{} }

func (r *InternetGatewayResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_internet_gateway"
}

func (r *InternetGatewayResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore internet gateway. Attach one to a VPC to enable internet routing for public subnets. API path: `/v1/internet-gateways`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "API-assigned internet gateway identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{Required: true, Description: "Internet gateway name."},
			"vpc_id": schema.StringAttribute{
				Required:    true,
				Description: "VPC to attach this internet gateway to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"status": schema.StringAttribute{Computed: true, Description: "Current internet gateway status (API-assigned)."},
			"created_at": schema.StringAttribute{
				Computed:    true,
				Description: "ISO 8601 timestamp when the internet gateway was created (API-assigned).",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"tags": schema.MapAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "Key/value tags to attach to the internet gateway.",
			},
		},
	}
}

func (r *InternetGatewayResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func igwMapToState(ctx context.Context, result igwAPIModel, state *InternetGatewayResourceModel) error {
	state.ID = types.StringValue(result.ID)
	state.Name = types.StringValue(result.Name)
	state.VPCID = types.StringValue(result.VPCID)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)
	tags, diags := tagsToMap(ctx, result.Tags)
	if diags.HasError() {
		return fmt.Errorf("converting tags")
	}
	state.Tags = tags
	return nil
}

func (r *InternetGatewayResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan InternetGatewayResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := igwAPIModel{
		Name:  plan.Name.ValueString(),
		VPCID: plan.VPCID.ValueString(),
		Tags:  tags,
	}

	var result igwAPIModel
	if err := r.client.Post(ctx, "/v1/internet-gateways", body, &result); err != nil {
		resp.Diagnostics.AddError("Create internet gateway failed", err.Error())
		return
	}
	if err := igwMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map internet gateway state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *InternetGatewayResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state InternetGatewayResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result igwAPIModel
	if err := r.client.Get(ctx, "/v1/internet-gateways/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read internet gateway failed", err.Error())
		return
	}
	if err := igwMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map internet gateway state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *InternetGatewayResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan InternetGatewayResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := igwAPIModel{
		Name: plan.Name.ValueString(),
		Tags: tags,
	}

	var result igwAPIModel
	if err := r.client.Put(ctx, "/v1/internet-gateways/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update internet gateway failed", err.Error())
		return
	}
	if err := igwMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map internet gateway state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *InternetGatewayResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state InternetGatewayResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/internet-gateways/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete internet gateway failed", err.Error())
	}
}

func (r *InternetGatewayResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var result igwAPIModel
	if err := r.client.Get(ctx, "/v1/internet-gateways/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import internet gateway failed", err.Error())
		return
	}
	var state InternetGatewayResourceModel
	if err := igwMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map internet gateway state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
