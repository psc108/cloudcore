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
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
	"github.com/hashicorp/terraform-plugin-framework/types/basetypes"
)

var _ resource.Resource = &LBListenerResource{}
var _ resource.ResourceWithImportState = &LBListenerResource{}

type LBListenerResource struct {
	client *client.Client
}

type lbRoutingRuleConditionsModel struct {
	PathPattern types.String `tfsdk:"path_pattern"`
	HostHeader  types.String `tfsdk:"host_header"`
}

type lbRoutingRuleModel struct {
	Priority      types.Int64  `tfsdk:"priority"`
	Conditions    types.Object `tfsdk:"conditions"`
	TargetGroupID types.String `tfsdk:"target_group_id"`
}

type LBListenerResourceModel struct {
	ID            types.String `tfsdk:"id"`
	LBID          types.String `tfsdk:"lb_id"`
	Port          types.Int64  `tfsdk:"port"`
	Protocol      types.String `tfsdk:"protocol"`
	TargetGroupID types.String `tfsdk:"target_group_id"`
	RoutingRules  types.List   `tfsdk:"routing_rules"`
	TLSCertARN    types.String `tfsdk:"tls_cert_arn"`
	Status        types.String `tfsdk:"status"`
}

type lbRuleConditionsAPIModel struct {
	PathPattern string `json:"path_pattern,omitempty"`
	HostHeader  string `json:"host_header,omitempty"`
}

type lbRoutingRuleAPIModel struct {
	Priority      int64                    `json:"priority"`
	Conditions    lbRuleConditionsAPIModel `json:"conditions"`
	TargetGroupID string                   `json:"target_group_id"`
}

type lbListenerAPIModel struct {
	ID            string                  `json:"id"`
	LBID          string                  `json:"lb_id"`
	Port          int64                   `json:"port"`
	Protocol      string                  `json:"protocol"`
	TargetGroupID string                  `json:"target_group_id"`
	RoutingRules  []lbRoutingRuleAPIModel `json:"routing_rules"`
	TLSCertARN    string                  `json:"tls_cert_arn,omitempty"`
	Status        string                  `json:"status"`
}

var lbRuleConditionsAttrTypes = map[string]attr.Type{
	"path_pattern": types.StringType,
	"host_header":  types.StringType,
}

var lbRoutingRuleAttrTypes = map[string]attr.Type{
	"priority":       types.Int64Type,
	"conditions":     types.ObjectType{AttrTypes: lbRuleConditionsAttrTypes},
	"target_group_id": types.StringType,
}

func NewLBListenerResource() resource.Resource { return &LBListenerResource{} }

func (r *LBListenerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_lb_listener"
}

func (r *LBListenerResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore load balancer listener — a port/protocol binding that forwards traffic to a target group. API path: `/v1/load-balancers/{lb_id}/listeners`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "API-assigned listener identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"lb_id": schema.StringAttribute{
				Required:    true,
				Description: "ID of the load balancer this listener belongs to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"port": schema.Int64Attribute{
				Required:    true,
				Description: "Port the listener accepts traffic on. Forces replacement on change.",
			},
			"protocol": schema.StringAttribute{
				Required:    true,
				Description: "Listener protocol: 'tcp', 'http', or 'https'. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
				Validators: []validator.String{
					stringvalidator.OneOf("tcp", "http", "https"),
				},
			},
			"target_group_id": schema.StringAttribute{
				Optional:    true,
				Computed:    true,
				Description: "ID of the default target group for this listener. Traffic not matched by routing_rules falls through to this group.",
			},
			"routing_rules": schema.ListNestedAttribute{
				Optional:    true,
				Computed:    true,
				Description: "Path/host-based routing rules evaluated in priority order before the default target group.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"priority": schema.Int64Attribute{
							Required:    true,
							Description: "Evaluation priority (lower numbers evaluated first).",
						},
						"conditions": schema.SingleNestedAttribute{
							Required:    true,
							Description: "Match conditions for this rule.",
							Attributes: map[string]schema.Attribute{
								"path_pattern": schema.StringAttribute{
									Optional:    true,
									Computed:    true,
									Description: "URL path pattern. Use '*' suffix for prefix match (e.g. '/api/*').",
								},
								"host_header": schema.StringAttribute{
									Optional:    true,
									Computed:    true,
									Description: "Exact Host header match (case-insensitive).",
								},
							},
						},
						"target_group_id": schema.StringAttribute{
							Required:    true,
							Description: "Target group to forward matched traffic to.",
						},
					},
				},
			},
			"tls_cert_arn": schema.StringAttribute{
				Optional:    true,
				Description: "ARN or identifier of the TLS certificate to use. Required when protocol is 'https'.",
			},
			"status": schema.StringAttribute{
				Computed:    true,
				Description: "Current listener status (API-assigned).",
			},
		},
	}
}

func (r *LBListenerResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func lbListenerMapToState(ctx context.Context, result lbListenerAPIModel, state *LBListenerResourceModel) error {
	state.ID = types.StringValue(result.ID)
	state.LBID = types.StringValue(result.LBID)
	state.Port = types.Int64Value(result.Port)
	state.Protocol = types.StringValue(result.Protocol)
	state.TargetGroupID = types.StringValue(result.TargetGroupID)
	state.Status = types.StringValue(result.Status)
	if result.TLSCertARN != "" {
		state.TLSCertARN = types.StringValue(result.TLSCertARN)
	}

	ruleElems := make([]attr.Value, len(result.RoutingRules))
	ruleObjType := types.ObjectType{AttrTypes: lbRoutingRuleAttrTypes}
	for i, r := range result.RoutingRules {
		condObj, diags := types.ObjectValue(lbRuleConditionsAttrTypes, map[string]attr.Value{
			"path_pattern": types.StringValue(r.Conditions.PathPattern),
			"host_header":  types.StringValue(r.Conditions.HostHeader),
		})
		if diags.HasError() {
			return fmt.Errorf("building rule conditions object")
		}
		ruleObj, diags := types.ObjectValue(lbRoutingRuleAttrTypes, map[string]attr.Value{
			"priority":        types.Int64Value(r.Priority),
			"conditions":      condObj,
			"target_group_id": types.StringValue(r.TargetGroupID),
		})
		if diags.HasError() {
			return fmt.Errorf("building routing rule object")
		}
		ruleElems[i] = ruleObj
	}
	rulesList, diags := types.ListValue(ruleObjType, ruleElems)
	if diags.HasError() {
		return fmt.Errorf("building routing_rules list")
	}
	state.RoutingRules = rulesList
	return nil
}

func lbListenerRulesToAPI(ctx context.Context, list types.List) ([]lbRoutingRuleAPIModel, error) {
	if list.IsNull() || list.IsUnknown() {
		return nil, nil
	}
	var models []lbRoutingRuleModel
	if diags := list.ElementsAs(ctx, &models, false); diags.HasError() {
		return nil, fmt.Errorf("parsing routing_rules")
	}
	out := make([]lbRoutingRuleAPIModel, len(models))
	for i, m := range models {
		var conds lbRoutingRuleConditionsModel
		if diags := m.Conditions.As(ctx, &conds, basetypes.ObjectAsOptions{}); diags.HasError() {
			return nil, fmt.Errorf("parsing rule conditions")
		}
		out[i] = lbRoutingRuleAPIModel{
			Priority: m.Priority.ValueInt64(),
			Conditions: lbRuleConditionsAPIModel{
				PathPattern: conds.PathPattern.ValueString(),
				HostHeader:  conds.HostHeader.ValueString(),
			},
			TargetGroupID: m.TargetGroupID.ValueString(),
		}
	}
	return out, nil
}

func (r *LBListenerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan LBListenerResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	rules, err := lbListenerRulesToAPI(ctx, plan.RoutingRules)
	if err != nil {
		resp.Diagnostics.AddError("Parse routing_rules failed", err.Error())
		return
	}
	body := lbListenerAPIModel{
		LBID:          plan.LBID.ValueString(),
		Port:          plan.Port.ValueInt64(),
		Protocol:      plan.Protocol.ValueString(),
		TargetGroupID: plan.TargetGroupID.ValueString(),
		RoutingRules:  rules,
		TLSCertARN:    plan.TLSCertARN.ValueString(),
	}

	var result lbListenerAPIModel
	path := "/v1/load-balancers/" + plan.LBID.ValueString() + "/listeners"
	if err := r.client.Post(ctx, path, body, &result); err != nil {
		resp.Diagnostics.AddError("Create listener failed", err.Error())
		return
	}
	if err := lbListenerMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map listener state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *LBListenerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state LBListenerResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	path := "/v1/load-balancers/" + state.LBID.ValueString() + "/listeners/" + state.ID.ValueString()
	var result lbListenerAPIModel
	if err := r.client.Get(ctx, path, &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read listener failed", err.Error())
		return
	}
	if err := lbListenerMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map listener state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *LBListenerResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan LBListenerResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	rules, err := lbListenerRulesToAPI(ctx, plan.RoutingRules)
	if err != nil {
		resp.Diagnostics.AddError("Parse routing_rules failed", err.Error())
		return
	}
	body := lbListenerAPIModel{
		LBID:          plan.LBID.ValueString(),
		Port:          plan.Port.ValueInt64(),
		Protocol:      plan.Protocol.ValueString(),
		TargetGroupID: plan.TargetGroupID.ValueString(),
		RoutingRules:  rules,
		TLSCertARN:    plan.TLSCertARN.ValueString(),
	}

	path := "/v1/load-balancers/" + plan.LBID.ValueString() + "/listeners/" + plan.ID.ValueString()
	var result lbListenerAPIModel
	if err := r.client.Put(ctx, path, body, &result); err != nil {
		resp.Diagnostics.AddError("Update listener failed", err.Error())
		return
	}
	if err := lbListenerMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map listener state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *LBListenerResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state LBListenerResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	path := "/v1/load-balancers/" + state.LBID.ValueString() + "/listeners/" + state.ID.ValueString()
	if err := r.client.Delete(ctx, path); err != nil {
		resp.Diagnostics.AddError("Delete listener failed", err.Error())
	}
}

// ImportState accepts "lb_id/listener_id".
func (r *LBListenerResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := splitTwo(req.ID)
	if parts == nil {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: lb_id/listener_id")
		return
	}
	lbID, listenerID := parts[0], parts[1]

	path := "/v1/load-balancers/" + lbID + "/listeners/" + listenerID
	var result lbListenerAPIModel
	if err := r.client.Get(ctx, path, &result); err != nil {
		resp.Diagnostics.AddError("Import listener failed", err.Error())
		return
	}
	var state LBListenerResourceModel
	if err := lbListenerMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map listener state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
