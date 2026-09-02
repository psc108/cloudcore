package resources

import (
	"context"
	"errors"
	"fmt"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &RouteTableResource{}
var _ resource.ResourceWithImportState = &RouteTableResource{}

type RouteTableResource struct {
	client *client.Client
}

type RouteTableResourceModel struct {
	ID        types.String `tfsdk:"id"`
	Name      types.String `tfsdk:"name"`
	VPCID     types.String `tfsdk:"vpc_id"`
	SubnetIDs types.List   `tfsdk:"subnet_ids"`
	Routes    types.List   `tfsdk:"routes"`
	Status    types.String `tfsdk:"status"`
	CreatedAt types.String `tfsdk:"created_at"`
	Tags      types.Map    `tfsdk:"tags"`
}

type rtRouteModel struct {
	DestinationCIDR types.String `tfsdk:"destination_cidr"`
	GatewayID       types.String `tfsdk:"gateway_id"`
}

var rtRouteAttrTypes = map[string]attr.Type{
	"destination_cidr": types.StringType,
	"gateway_id":       types.StringType,
}

type rtAPIModel struct {
	ID        string            `json:"id"`
	Name      string            `json:"name"`
	VPCID     string            `json:"vpc_id"`
	SubnetIDs []string          `json:"subnet_ids"`
	Routes    []rtAPIRoute      `json:"routes"`
	Status    string            `json:"status"`
	CreatedAt string            `json:"created_at"`
	Tags      map[string]string `json:"tags"`
}

type rtAPIRoute struct {
	DestinationCIDR string `json:"destination_cidr"`
	GatewayID       string `json:"gateway_id"`
}

func NewRouteTableResource() resource.Resource { return &RouteTableResource{} }

func (r *RouteTableResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_route_table"
}

func (r *RouteTableResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore route table. Associate subnets and define routes (e.g. 0.0.0.0/0 → internet gateway) to control traffic flow. API path: `/v1/route-tables`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "API-assigned route table identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{Required: true, Description: "Route table name."},
			"vpc_id": schema.StringAttribute{
				Required:    true,
				Description: "VPC this route table belongs to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"subnet_ids": schema.ListAttribute{
				Optional:    true,
				Computed:    true,
				ElementType: types.StringType,
				Description: "Subnet IDs associated with this route table.",
			},
			"routes": schema.ListNestedAttribute{
				Optional:    true,
				Computed:    true,
				Description: "List of routes in this table.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"destination_cidr": schema.StringAttribute{
							Required:    true,
							Description: "Destination CIDR block (e.g. '0.0.0.0/0' for a default route).",
						},
						"gateway_id": schema.StringAttribute{
							Required:    true,
							Description: "Target gateway ID, or 'local' for VPC-local routing.",
						},
					},
				},
			},
			"status": schema.StringAttribute{Computed: true, Description: "Current route table status (API-assigned)."},
			"created_at": schema.StringAttribute{
				Computed:    true,
				Description: "ISO 8601 timestamp when the route table was created (API-assigned).",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"tags": schema.MapAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "Key/value tags to attach to the route table.",
			},
		},
	}
}

func (r *RouteTableResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func rtMapToState(ctx context.Context, result rtAPIModel, state *RouteTableResourceModel) error {
	state.ID = types.StringValue(result.ID)
	state.Name = types.StringValue(result.Name)
	state.VPCID = types.StringValue(result.VPCID)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)

	subnetIDs, diags := stringsToList(ctx, result.SubnetIDs)
	if diags.HasError() {
		return fmt.Errorf("converting subnet_ids")
	}
	state.SubnetIDs = subnetIDs

	routeObjs := make([]attr.Value, len(result.Routes))
	for i, r := range result.Routes {
		obj, d := types.ObjectValue(rtRouteAttrTypes, map[string]attr.Value{
			"destination_cidr": types.StringValue(r.DestinationCIDR),
			"gateway_id":       types.StringValue(r.GatewayID),
		})
		if d.HasError() {
			return fmt.Errorf("converting route %d", i)
		}
		routeObjs[i] = obj
	}
	routes, diags := types.ListValue(types.ObjectType{AttrTypes: rtRouteAttrTypes}, routeObjs)
	if diags.HasError() {
		return fmt.Errorf("building routes list")
	}
	state.Routes = routes

	tags, diags := tagsToMap(ctx, result.Tags)
	if diags.HasError() {
		return fmt.Errorf("converting tags")
	}
	state.Tags = tags
	return nil
}

func rtPlanToAPI(ctx context.Context, plan RouteTableResourceModel) (rtAPIModel, error) {
	tags := map[string]string{}
	if diags := plan.Tags.ElementsAs(ctx, &tags, false); diags.HasError() {
		return rtAPIModel{}, fmt.Errorf("reading tags")
	}

	subnetIDs := []string{}
	if diags := plan.SubnetIDs.ElementsAs(ctx, &subnetIDs, false); diags.HasError() {
		return rtAPIModel{}, fmt.Errorf("reading subnet_ids")
	}

	var routeModels []rtRouteModel
	if diags := plan.Routes.ElementsAs(ctx, &routeModels, false); diags.HasError() {
		return rtAPIModel{}, fmt.Errorf("reading routes")
	}
	routes := make([]rtAPIRoute, len(routeModels))
	for i, rm := range routeModels {
		routes[i] = rtAPIRoute{
			DestinationCIDR: rm.DestinationCIDR.ValueString(),
			GatewayID:       rm.GatewayID.ValueString(),
		}
	}

	return rtAPIModel{
		Name:      plan.Name.ValueString(),
		VPCID:     plan.VPCID.ValueString(),
		SubnetIDs: subnetIDs,
		Routes:    routes,
		Tags:      tags,
	}, nil
}

func (r *RouteTableResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan RouteTableResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body, err := rtPlanToAPI(ctx, plan)
	if err != nil {
		resp.Diagnostics.AddError("Build route table request failed", err.Error())
		return
	}

	var result rtAPIModel
	if err := r.client.Post(ctx, "/v1/route-tables", body, &result); err != nil {
		resp.Diagnostics.AddError("Create route table failed", err.Error())
		return
	}
	if err := rtMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map route table state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *RouteTableResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state RouteTableResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result rtAPIModel
	if err := r.client.Get(ctx, "/v1/route-tables/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read route table failed", err.Error())
		return
	}
	if err := rtMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map route table state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *RouteTableResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan RouteTableResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body, err := rtPlanToAPI(ctx, plan)
	if err != nil {
		resp.Diagnostics.AddError("Build route table request failed", err.Error())
		return
	}

	var result rtAPIModel
	if err := r.client.Put(ctx, "/v1/route-tables/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update route table failed", err.Error())
		return
	}
	if err := rtMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map route table state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *RouteTableResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state RouteTableResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/route-tables/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete route table failed", err.Error())
	}
}

func (r *RouteTableResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var result rtAPIModel
	if err := r.client.Get(ctx, "/v1/route-tables/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import route table failed", err.Error())
		return
	}
	var state RouteTableResourceModel
	if err := rtMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map route table state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

