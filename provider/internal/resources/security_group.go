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
)

var _ resource.Resource = &SecurityGroupResource{}
var _ resource.ResourceWithImportState = &SecurityGroupResource{}

type SecurityGroupResource struct {
	client *client.Client
}

type sgRuleModel struct {
	Protocol    types.String `tfsdk:"protocol"`
	FromPort    types.Int64  `tfsdk:"from_port"`
	ToPort      types.Int64  `tfsdk:"to_port"`
	CIDR        types.String `tfsdk:"cidr"`
	Description types.String `tfsdk:"description"`
}

type SecurityGroupResourceModel struct {
	ID           types.String `tfsdk:"id"`
	Name         types.String `tfsdk:"name"`
	Description  types.String `tfsdk:"description"`
	VPCID        types.String `tfsdk:"vpc_id"`
	IngressRules types.List   `tfsdk:"ingress_rules"`
	EgressRules  types.List   `tfsdk:"egress_rules"`
	Status       types.String `tfsdk:"status"`
	CreatedAt    types.String `tfsdk:"created_at"`
	Tags         types.Map    `tfsdk:"tags"`
}

type sgRuleAPIModel struct {
	Protocol    string `json:"protocol"`
	FromPort    *int64 `json:"from_port,omitempty"`
	ToPort      *int64 `json:"to_port,omitempty"`
	CIDR        string `json:"cidr"`
	Description string `json:"description,omitempty"`
}

type sgAPIModel struct {
	ID           string            `json:"id"`
	Name         string            `json:"name"`
	Description  string            `json:"description"`
	VPCID        string            `json:"vpc_id"`
	IngressRules []sgRuleAPIModel  `json:"ingress_rules"`
	EgressRules  []sgRuleAPIModel  `json:"egress_rules"`
	Status       string            `json:"status"`
	CreatedAt    string            `json:"created_at"`
	Tags         map[string]string `json:"tags"`
}

var sgRuleAttrTypes = map[string]attr.Type{
	"protocol":    types.StringType,
	"from_port":   types.Int64Type,
	"to_port":     types.Int64Type,
	"cidr":        types.StringType,
	"description": types.StringType,
}

func NewSecurityGroupResource() resource.Resource { return &SecurityGroupResource{} }

func (r *SecurityGroupResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_security_group"
}

func (r *SecurityGroupResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	ruleSchema := schema.NestedAttributeObject{
		Attributes: map[string]schema.Attribute{
			"protocol": schema.StringAttribute{
				Required:    true,
				Description: "Protocol: tcp, udp, icmp, or -1 (all traffic).",
				Validators: []validator.String{
					stringvalidator.OneOf("tcp", "udp", "icmp", "-1"),
				},
			},
			"from_port":   schema.Int64Attribute{Optional: true, Description: "Start of port range (inclusive)."},
			"to_port":     schema.Int64Attribute{Optional: true, Description: "End of port range (inclusive)."},
			"cidr":        schema.StringAttribute{Required: true, Description: "Source (ingress) or destination (egress) CIDR block."},
			"description": schema.StringAttribute{Optional: true, Description: "Human-readable rule description."},
		},
	}
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore security group with ingress and egress rules. API path: `/v1/security-groups`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:      true,
				Description:   "API-assigned security group identifier.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"name": schema.StringAttribute{Required: true, Description: "Security group name."},
			"description": schema.StringAttribute{
				Optional:    true,
				Computed:    true,
				Description: "Human-readable description of the security group.",
			},
			"vpc_id": schema.StringAttribute{
				Required:      true,
				Description:   "VPC the security group belongs to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"ingress_rules": schema.ListNestedAttribute{
				Optional:     true,
				Description:  "Inbound traffic rules.",
				NestedObject: ruleSchema,
			},
			"egress_rules": schema.ListNestedAttribute{
				Optional:     true,
				Description:  "Outbound traffic rules.",
				NestedObject: ruleSchema,
			},
			"status":     schema.StringAttribute{Computed: true, Description: "Current security group status (API-assigned)."},
			"created_at": schema.StringAttribute{
				Computed:      true,
				Description:   "ISO 8601 timestamp when the security group was created (API-assigned).",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"tags": schema.MapAttribute{Optional: true, ElementType: types.StringType, Description: "Key/value tags to attach to the security group."},
		},
	}
}

func (r *SecurityGroupResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *SecurityGroupResource) rulesFromAPI(_ context.Context, apiRules []sgRuleAPIModel) (types.List, error) {
	if len(apiRules) == 0 {
		return types.ListNull(types.ObjectType{AttrTypes: sgRuleAttrTypes}), nil
	}
	elems := make([]attr.Value, len(apiRules))
	for i, rule := range apiRules {
		var fp, tp types.Int64
		if rule.FromPort != nil {
			fp = types.Int64Value(*rule.FromPort)
		} else {
			fp = types.Int64Null()
		}
		if rule.ToPort != nil {
			tp = types.Int64Value(*rule.ToPort)
		} else {
			tp = types.Int64Null()
		}
		obj, diags := types.ObjectValue(sgRuleAttrTypes, map[string]attr.Value{
			"protocol":    types.StringValue(rule.Protocol),
			"from_port":   fp,
			"to_port":     tp,
			"cidr":        types.StringValue(rule.CIDR),
			"description": types.StringValue(rule.Description),
		})
		if diags.HasError() {
			return types.ListNull(types.ObjectType{AttrTypes: sgRuleAttrTypes}), fmt.Errorf("building rule object")
		}
		elems[i] = obj
	}
	list, diags := types.ListValue(types.ObjectType{AttrTypes: sgRuleAttrTypes}, elems)
	if diags.HasError() {
		return types.ListNull(types.ObjectType{AttrTypes: sgRuleAttrTypes}), fmt.Errorf("building rules list")
	}
	return list, nil
}

func (r *SecurityGroupResource) rulesToAPI(ctx context.Context, list types.List) ([]sgRuleAPIModel, error) {
	var models []sgRuleModel
	if diags := list.ElementsAs(ctx, &models, false); diags.HasError() {
		return nil, fmt.Errorf("parsing rules")
	}
	out := make([]sgRuleAPIModel, len(models))
	for i, m := range models {
		rule := sgRuleAPIModel{
			Protocol:    m.Protocol.ValueString(),
			CIDR:        m.CIDR.ValueString(),
			Description: m.Description.ValueString(),
		}
		if !m.FromPort.IsNull() && !m.FromPort.IsUnknown() {
			v := m.FromPort.ValueInt64()
			rule.FromPort = &v
		}
		if !m.ToPort.IsNull() && !m.ToPort.IsUnknown() {
			v := m.ToPort.ValueInt64()
			rule.ToPort = &v
		}
		out[i] = rule
	}
	return out, nil
}

func (r *SecurityGroupResource) sgMapToState(ctx context.Context, result sgAPIModel, state *SecurityGroupResourceModel) error {
	state.ID = types.StringValue(result.ID)
	state.Name = types.StringValue(result.Name)
	state.Description = types.StringValue(result.Description)
	state.VPCID = types.StringValue(result.VPCID)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)
	tags, diags := tagsToMap(ctx, result.Tags)
	if diags.HasError() {
		return fmt.Errorf("converting tags")
	}
	state.Tags = tags
	ingressList, err := r.rulesFromAPI(ctx, result.IngressRules)
	if err != nil {
		return fmt.Errorf("converting ingress rules: %w", err)
	}
	state.IngressRules = ingressList
	egressList, err := r.rulesFromAPI(ctx, result.EgressRules)
	if err != nil {
		return fmt.Errorf("converting egress rules: %w", err)
	}
	state.EgressRules = egressList
	return nil
}

func (r *SecurityGroupResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan SecurityGroupResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	ingress, err := r.rulesToAPI(ctx, plan.IngressRules)
	if err != nil {
		resp.Diagnostics.AddError("Parse ingress rules failed", err.Error())
		return
	}
	egress, err := r.rulesToAPI(ctx, plan.EgressRules)
	if err != nil {
		resp.Diagnostics.AddError("Parse egress rules failed", err.Error())
		return
	}

	body := sgAPIModel{
		Name:         plan.Name.ValueString(),
		Description:  plan.Description.ValueString(),
		VPCID:        plan.VPCID.ValueString(),
		IngressRules: ingress,
		EgressRules:  egress,
		Tags:         tags,
	}
	var result sgAPIModel
	if err := r.client.Post(ctx, "/v1/security-groups", body, &result); err != nil {
		resp.Diagnostics.AddError("Create security group failed", err.Error())
		return
	}
	if err := r.sgMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map security group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *SecurityGroupResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state SecurityGroupResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	var result sgAPIModel
	if err := r.client.Get(ctx, "/v1/security-groups/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read security group failed", err.Error())
		return
	}
	if err := r.sgMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map security group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *SecurityGroupResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan SecurityGroupResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	ingress, err := r.rulesToAPI(ctx, plan.IngressRules)
	if err != nil {
		resp.Diagnostics.AddError("Parse ingress rules failed", err.Error())
		return
	}
	egress, err := r.rulesToAPI(ctx, plan.EgressRules)
	if err != nil {
		resp.Diagnostics.AddError("Parse egress rules failed", err.Error())
		return
	}
	body := sgAPIModel{
		Description:  plan.Description.ValueString(),
		IngressRules: ingress,
		EgressRules:  egress,
		Tags:         tags,
	}
	var result sgAPIModel
	if err := r.client.Put(ctx, "/v1/security-groups/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update security group failed", err.Error())
		return
	}
	if err := r.sgMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map security group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *SecurityGroupResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state SecurityGroupResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/security-groups/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete security group failed", err.Error())
	}
}

func (r *SecurityGroupResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var result sgAPIModel
	if err := r.client.Get(ctx, "/v1/security-groups/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import security group failed", err.Error())
		return
	}
	var state SecurityGroupResourceModel
	if err := r.sgMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map security group state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
