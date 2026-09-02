package resources

import (
	"context"
	"errors"
	"fmt"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework-validators/stringvalidator"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-framework/types/basetypes"
)

var _ resource.Resource = &LBTargetGroupResource{}
var _ resource.ResourceWithImportState = &LBTargetGroupResource{}

type LBTargetGroupResource struct {
	client *client.Client
}

type lbTGTargetModel struct {
	InstanceID types.String `tfsdk:"instance_id"`
	Port       types.Int64  `tfsdk:"port"`
}

type lbTGHealthCheckModel struct {
	Path               types.String `tfsdk:"path"`
	Interval           types.Int64  `tfsdk:"interval"`
	HealthyThreshold   types.Int64  `tfsdk:"healthy_threshold"`
	UnhealthyThreshold types.Int64  `tfsdk:"unhealthy_threshold"`
}

type LBTargetGroupResourceModel struct {
	ID          types.String `tfsdk:"id"`
	LBID        types.String `tfsdk:"lb_id"`
	Name        types.String `tfsdk:"name"`
	Port        types.Int64  `tfsdk:"port"`
	Protocol    types.String `tfsdk:"protocol"`
	HealthCheck types.Object `tfsdk:"health_check"`
	Targets     types.List   `tfsdk:"targets"`
	Status      types.String `tfsdk:"status"`
}

type lbTGTargetAPIModel struct {
	InstanceID string `json:"instance_id"`
	Port       int64  `json:"port"`
}

type lbTGHealthCheckAPIModel struct {
	Path               string `json:"path,omitempty"`
	Interval           int64  `json:"interval"`
	HealthyThreshold   int64  `json:"healthy_threshold"`
	UnhealthyThreshold int64  `json:"unhealthy_threshold"`
}

type lbTGAPIModel struct {
	ID          string                  `json:"id"`
	LBID        string                  `json:"lb_id"`
	Name        string                  `json:"name"`
	Port        int64                   `json:"port"`
	Protocol    string                  `json:"protocol"`
	HealthCheck lbTGHealthCheckAPIModel `json:"health_check"`
	Targets     []lbTGTargetAPIModel    `json:"targets"`
	Status      string                  `json:"status"`
}

var lbTGTargetAttrTypes = map[string]attr.Type{
	"instance_id": types.StringType,
	"port":        types.Int64Type,
}

var lbTGHealthCheckAttrTypes = map[string]attr.Type{
	"path":                types.StringType,
	"interval":            types.Int64Type,
	"healthy_threshold":   types.Int64Type,
	"unhealthy_threshold": types.Int64Type,
}

func NewLBTargetGroupResource() resource.Resource { return &LBTargetGroupResource{} }

func (r *LBTargetGroupResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_lb_target_group"
}

func (r *LBTargetGroupResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore load balancer target group — a named pool of instances that a listener routes traffic to. API path: `/v1/load-balancers/{lb_id}/target-groups`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "API-assigned target group identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"lb_id": schema.StringAttribute{
				Required:    true,
				Description: "ID of the load balancer this target group belongs to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"name": schema.StringAttribute{
				Required:    true,
				Description: "Target group name.",
			},
			"port": schema.Int64Attribute{
				Required:    true,
				Description: "Default port traffic is forwarded to on each target.",
			},
			"protocol": schema.StringAttribute{
				Required:    true,
				Description: "Routing protocol: 'tcp', 'http', or 'https'.",
				Validators: []validator.String{
					stringvalidator.OneOf("tcp", "http", "https"),
				},
			},
			"health_check": schema.SingleNestedAttribute{
				Optional:    true,
				Computed:    true,
				Description: "Health check configuration. Defaults are applied by the API when omitted.",
				Attributes: map[string]schema.Attribute{
					"path": schema.StringAttribute{
						Optional:    true,
						Computed:    true,
						Description: "HTTP path for health checks (http/https only). Defaults to '/'.",
					},
					"interval": schema.Int64Attribute{
						Optional:    true,
						Computed:    true,
						Description: "Seconds between health checks. Defaults to 30.",
						Default:     int64default.StaticInt64(30),
					},
					"healthy_threshold": schema.Int64Attribute{
						Optional:    true,
						Computed:    true,
						Description: "Consecutive successes before a target is considered healthy. Defaults to 2.",
						Default:     int64default.StaticInt64(2),
					},
					"unhealthy_threshold": schema.Int64Attribute{
						Optional:    true,
						Computed:    true,
						Description: "Consecutive failures before a target is considered unhealthy. Defaults to 2.",
						Default:     int64default.StaticInt64(2),
					},
				},
			},
			"targets": schema.ListNestedAttribute{
				Optional:    true,
				Description: "Instances registered in this target group.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"instance_id": schema.StringAttribute{
							Required:    true,
							Description: "ID of the instance to register as a target.",
						},
						"port": schema.Int64Attribute{
							Optional:    true,
							Computed:    true,
							Description: "Port override for this target. Defaults to the target group port.",
						},
					},
				},
			},
			"status": schema.StringAttribute{
				Computed:    true,
				Description: "Current target group status (API-assigned).",
			},
		},
	}
}

func (r *LBTargetGroupResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func lbTGMapToState(ctx context.Context, result lbTGAPIModel, state *LBTargetGroupResourceModel) error {
	state.ID = types.StringValue(result.ID)
	state.LBID = types.StringValue(result.LBID)
	state.Name = types.StringValue(result.Name)
	state.Port = types.Int64Value(result.Port)
	state.Protocol = types.StringValue(result.Protocol)
	state.Status = types.StringValue(result.Status)

	hc, diags := types.ObjectValue(lbTGHealthCheckAttrTypes, map[string]attr.Value{
		"path":                types.StringValue(result.HealthCheck.Path),
		"interval":            types.Int64Value(result.HealthCheck.Interval),
		"healthy_threshold":   types.Int64Value(result.HealthCheck.HealthyThreshold),
		"unhealthy_threshold": types.Int64Value(result.HealthCheck.UnhealthyThreshold),
	})
	if diags.HasError() {
		return fmt.Errorf("building health_check object")
	}
	state.HealthCheck = hc

	elems := make([]attr.Value, len(result.Targets))
	for i, t := range result.Targets {
		obj, d := types.ObjectValue(lbTGTargetAttrTypes, map[string]attr.Value{
			"instance_id": types.StringValue(t.InstanceID),
			"port":        types.Int64Value(t.Port),
		})
		if d.HasError() {
			return fmt.Errorf("building target object")
		}
		elems[i] = obj
	}
	targets, diags := types.ListValue(types.ObjectType{AttrTypes: lbTGTargetAttrTypes}, elems)
	if diags.HasError() {
		return fmt.Errorf("building targets list")
	}
	state.Targets = targets
	return nil
}

func planToLBTGBody(ctx context.Context, plan LBTargetGroupResourceModel) (lbTGAPIModel, error) {
	body := lbTGAPIModel{
		LBID:     plan.LBID.ValueString(),
		Name:     plan.Name.ValueString(),
		Port:     plan.Port.ValueInt64(),
		Protocol: plan.Protocol.ValueString(),
	}

	if !plan.HealthCheck.IsNull() && !plan.HealthCheck.IsUnknown() {
		var hc lbTGHealthCheckModel
		if diags := plan.HealthCheck.As(ctx, &hc, basetypes.ObjectAsOptions{}); diags.HasError() {
			return body, fmt.Errorf("reading health_check")
		}
		body.HealthCheck = lbTGHealthCheckAPIModel{
			Path:               hc.Path.ValueString(),
			Interval:           hc.Interval.ValueInt64(),
			HealthyThreshold:   hc.HealthyThreshold.ValueInt64(),
			UnhealthyThreshold: hc.UnhealthyThreshold.ValueInt64(),
		}
	}

	var planTargets []lbTGTargetModel
	if diags := plan.Targets.ElementsAs(ctx, &planTargets, false); diags.HasError() {
		return body, fmt.Errorf("reading targets")
	}
	body.Targets = make([]lbTGTargetAPIModel, len(planTargets))
	for i, t := range planTargets {
		body.Targets[i] = lbTGTargetAPIModel{
			InstanceID: t.InstanceID.ValueString(),
			Port:       t.Port.ValueInt64(),
		}
	}
	return body, nil
}

func (r *LBTargetGroupResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan LBTargetGroupResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body, err := planToLBTGBody(ctx, plan)
	if err != nil {
		resp.Diagnostics.AddError("Build target group request failed", err.Error())
		return
	}

	var result lbTGAPIModel
	path := "/v1/load-balancers/" + plan.LBID.ValueString() + "/target-groups"
	if err := r.client.Post(ctx, path, body, &result); err != nil {
		resp.Diagnostics.AddError("Create target group failed", err.Error())
		return
	}
	if err := lbTGMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map target group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *LBTargetGroupResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state LBTargetGroupResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	path := "/v1/load-balancers/" + state.LBID.ValueString() + "/target-groups/" + state.ID.ValueString()
	var result lbTGAPIModel
	if err := r.client.Get(ctx, path, &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read target group failed", err.Error())
		return
	}
	if err := lbTGMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map target group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *LBTargetGroupResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan LBTargetGroupResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body, err := planToLBTGBody(ctx, plan)
	if err != nil {
		resp.Diagnostics.AddError("Build target group request failed", err.Error())
		return
	}

	path := "/v1/load-balancers/" + plan.LBID.ValueString() + "/target-groups/" + plan.ID.ValueString()
	var result lbTGAPIModel
	if err := r.client.Put(ctx, path, body, &result); err != nil {
		resp.Diagnostics.AddError("Update target group failed", err.Error())
		return
	}
	if err := lbTGMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map target group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *LBTargetGroupResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state LBTargetGroupResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	path := "/v1/load-balancers/" + state.LBID.ValueString() + "/target-groups/" + state.ID.ValueString()
	if err := r.client.Delete(ctx, path); err != nil {
		resp.Diagnostics.AddError("Delete target group failed", err.Error())
	}
}

// ImportState accepts "lb_id/tg_id".
func (r *LBTargetGroupResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := splitTwo(req.ID)
	if parts == nil {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: lb_id/tg_id")
		return
	}
	lbID, tgID := parts[0], parts[1]

	path := "/v1/load-balancers/" + lbID + "/target-groups/" + tgID
	var result lbTGAPIModel
	if err := r.client.Get(ctx, path, &result); err != nil {
		resp.Diagnostics.AddError("Import target group failed", err.Error())
		return
	}
	var state LBTargetGroupResourceModel
	if err := lbTGMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map target group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
