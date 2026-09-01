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

var _ resource.Resource = &InstanceResource{}
var _ resource.ResourceWithImportState = &InstanceResource{}

type InstanceResource struct {
	client *client.Client
}

type InstanceResourceModel struct {
	ID               types.String `tfsdk:"id"`
	Name             types.String `tfsdk:"name"`
	ImageID          types.String `tfsdk:"image_id"`
	Flavor           types.String `tfsdk:"flavor"`
	VPCID            types.String `tfsdk:"vpc_id"`
	SubnetID         types.String `tfsdk:"subnet_id"`
	SecurityGroupIDs types.List   `tfsdk:"security_group_ids"`
	UserData         types.String `tfsdk:"user_data"`
	PrivateIP        types.String `tfsdk:"private_ip"`
	PublicIP         types.String `tfsdk:"public_ip"`
	Status           types.String `tfsdk:"status"`
	CreatedAt        types.String `tfsdk:"created_at"`
	Tags             types.Map    `tfsdk:"tags"`
}

type instanceAPIModel struct {
	ID               string            `json:"id"`
	Name             string            `json:"name"`
	ImageID          string            `json:"image_id"`
	Flavor           string            `json:"flavor"`
	VPCID            string            `json:"vpc_id"`
	SubnetID         string            `json:"subnet_id"`
	SecurityGroupIDs []string          `json:"security_group_ids"`
	UserData         string            `json:"user_data,omitempty"`
	PrivateIP        string            `json:"private_ip"`
	PublicIP         string            `json:"public_ip"`
	Status           string            `json:"status"`
	CreatedAt        string            `json:"created_at"`
	Tags             map[string]string `json:"tags"`
}

func NewInstanceResource() resource.Resource { return &InstanceResource{} }

func (r *InstanceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_instance"
}

func (r *InstanceResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name":     schema.StringAttribute{Required: true},
			"image_id": schema.StringAttribute{Required: true},
			"flavor":   schema.StringAttribute{Required: true},
			"vpc_id": schema.StringAttribute{
				Required: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"subnet_id": schema.StringAttribute{
				Required: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"security_group_ids": schema.ListAttribute{
				Optional:    true,
				ElementType: types.StringType,
			},
			"user_data": schema.StringAttribute{
				Optional:  true,
				Sensitive: true,
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"private_ip": schema.StringAttribute{Computed: true},
			"public_ip":  schema.StringAttribute{Computed: true},
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

func (r *InstanceResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *InstanceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan InstanceResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	sgIDs := []string{}
	resp.Diagnostics.Append(plan.SecurityGroupIDs.ElementsAs(ctx, &sgIDs, false)...)
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := instanceAPIModel{
		Name:             plan.Name.ValueString(),
		ImageID:          plan.ImageID.ValueString(),
		Flavor:           plan.Flavor.ValueString(),
		VPCID:            plan.VPCID.ValueString(),
		SubnetID:         plan.SubnetID.ValueString(),
		SecurityGroupIDs: sgIDs,
		UserData:         plan.UserData.ValueString(),
		Tags:             tags,
	}

	var result instanceAPIModel
	if err := r.client.Post(ctx, "/v1/instances", body, &result); err != nil {
		resp.Diagnostics.AddError("Create instance failed", err.Error())
		return
	}

	plan.ID = types.StringValue(result.ID)
	plan.PrivateIP = types.StringValue(result.PrivateIP)
	plan.PublicIP = types.StringValue(result.PublicIP)
	plan.Status = types.StringValue(result.Status)
	plan.CreatedAt = types.StringValue(result.CreatedAt)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *InstanceResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state InstanceResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result instanceAPIModel
	if err := r.client.Get(ctx, "/v1/instances/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read instance failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.ImageID = types.StringValue(result.ImageID)
	state.Flavor = types.StringValue(result.Flavor)
	state.VPCID = types.StringValue(result.VPCID)
	state.SubnetID = types.StringValue(result.SubnetID)
	state.PrivateIP = types.StringValue(result.PrivateIP)
	state.PublicIP = types.StringValue(result.PublicIP)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)

	sgIDs, diags := types.ListValueFrom(ctx, types.StringType, result.SecurityGroupIDs)
	resp.Diagnostics.Append(diags...)
	state.SecurityGroupIDs = sgIDs

	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags

	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *InstanceResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan InstanceResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	sgIDs := []string{}
	resp.Diagnostics.Append(plan.SecurityGroupIDs.ElementsAs(ctx, &sgIDs, false)...)
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := instanceAPIModel{
		Name:             plan.Name.ValueString(),
		ImageID:          plan.ImageID.ValueString(),
		Flavor:           plan.Flavor.ValueString(),
		VPCID:            plan.VPCID.ValueString(),
		SubnetID:         plan.SubnetID.ValueString(),
		SecurityGroupIDs: sgIDs,
		Tags:             tags,
	}

	var result instanceAPIModel
	if err := r.client.Put(ctx, "/v1/instances/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update instance failed", err.Error())
		return
	}

	plan.PrivateIP = types.StringValue(result.PrivateIP)
	plan.PublicIP = types.StringValue(result.PublicIP)
	plan.Status = types.StringValue(result.Status)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *InstanceResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state InstanceResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/instances/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete instance failed", err.Error())
	}
}

func (r *InstanceResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var state InstanceResourceModel
	state.ID = types.StringValue(req.ID)

	var result instanceAPIModel
	if err := r.client.Get(ctx, "/v1/instances/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import instance failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.ImageID = types.StringValue(result.ImageID)
	state.Flavor = types.StringValue(result.Flavor)
	state.VPCID = types.StringValue(result.VPCID)
	state.SubnetID = types.StringValue(result.SubnetID)
	state.PrivateIP = types.StringValue(result.PrivateIP)
	state.PublicIP = types.StringValue(result.PublicIP)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)

	sgIDs, diags := types.ListValueFrom(ctx, types.StringType, result.SecurityGroupIDs)
	resp.Diagnostics.Append(diags...)
	state.SecurityGroupIDs = sgIDs

	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags

	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
