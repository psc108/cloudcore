package datasources

import (
	"context"
	"fmt"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ── VPC data source ──────────────────────────────────────────────────────────

var _ datasource.DataSource = &VPCDataSource{}

type VPCDataSource struct{ client *client.Client }

type VPCDataSourceModel struct {
	ID         types.String `tfsdk:"id"`
	Name       types.String `tfsdk:"name"`
	CIDRBlock  types.String `tfsdk:"cidr_block"`
	DNSSupport types.Bool   `tfsdk:"dns_support"`
	Status     types.String `tfsdk:"status"`
	Tags       types.Map    `tfsdk:"tags"`
}

type vpcItemAPI struct {
	ID         string            `json:"id"`
	Name       string            `json:"name"`
	CIDRBlock  string            `json:"cidr_block"`
	DNSSupport bool              `json:"dns_support"`
	Status     string            `json:"status"`
	Tags       map[string]string `json:"tags"`
}

func NewVPCDataSource() datasource.DataSource { return &VPCDataSource{} }

func (d *VPCDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_vpc"
}

func (d *VPCDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Fetches a CloudCore VPC by ID or name.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Optional: true, Computed: true},
			"name":        schema.StringAttribute{Optional: true, Computed: true},
			"cidr_block":  schema.StringAttribute{Computed: true},
			"dns_support": schema.BoolAttribute{Computed: true},
			"status":      schema.StringAttribute{Computed: true},
			"tags":        schema.MapAttribute{Computed: true, ElementType: types.StringType},
		},
	}
}

func (d *VPCDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("got %T", req.ProviderData))
		return
	}
	d.client = c
}

func (d *VPCDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config VPCDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if (config.ID.IsNull() || config.ID.ValueString() == "") && (config.Name.IsNull() || config.Name.ValueString() == "") {
		resp.Diagnostics.AddError("Missing lookup key", "At least one of 'id' or 'name' must be set.")
		return
	}

	set := func(v vpcItemAPI) {
		tags, diags := types.MapValueFrom(ctx, types.StringType, v.Tags)
		resp.Diagnostics.Append(diags...)
		resp.Diagnostics.Append(resp.State.Set(ctx, &VPCDataSourceModel{
			ID: types.StringValue(v.ID), Name: types.StringValue(v.Name),
			CIDRBlock: types.StringValue(v.CIDRBlock), DNSSupport: types.BoolValue(v.DNSSupport),
			Status: types.StringValue(v.Status), Tags: tags,
		})...)
	}

	if !config.ID.IsNull() && config.ID.ValueString() != "" {
		var result vpcItemAPI
		if err := d.client.Get(ctx, "/v1/vpcs/"+config.ID.ValueString(), &result); err != nil {
			resp.Diagnostics.AddError("Read VPC failed", err.Error())
			return
		}
		set(result)
		return
	}

	var list struct {
		Items []vpcItemAPI `json:"items"`
	}
	if err := d.client.Get(ctx, "/v1/vpcs", &list); err != nil {
		resp.Diagnostics.AddError("List VPCs failed", err.Error())
		return
	}
	for _, v := range list.Items {
		if config.Name.IsNull() || v.Name == config.Name.ValueString() {
			set(v)
			return
		}
	}
	resp.Diagnostics.AddError("VPC not found", fmt.Sprintf("no VPC matching id=%q name=%q",
		config.ID.ValueString(), config.Name.ValueString()))
}

// ── Instance data source ─────────────────────────────────────────────────────

var _ datasource.DataSource = &InstanceDataSource{}

type InstanceDataSource struct{ client *client.Client }

type InstanceDataSourceModel struct {
	ID               types.String `tfsdk:"id"`
	Name             types.String `tfsdk:"name"`
	ImageID          types.String `tfsdk:"image_id"`
	Flavor           types.String `tfsdk:"flavor"`
	VPCID            types.String `tfsdk:"vpc_id"`
	SubnetID         types.String `tfsdk:"subnet_id"`
	SecurityGroupIDs types.List   `tfsdk:"security_group_ids"`
	PrivateIP        types.String `tfsdk:"private_ip"`
	PublicIP         types.String `tfsdk:"public_ip"`
	Status           types.String `tfsdk:"status"`
	Tags             types.Map    `tfsdk:"tags"`
}

type instanceItemAPI struct {
	ID               string            `json:"id"`
	Name             string            `json:"name"`
	ImageID          string            `json:"image_id"`
	Flavor           string            `json:"flavor"`
	VPCID            string            `json:"vpc_id"`
	SubnetID         string            `json:"subnet_id"`
	SecurityGroupIDs []string          `json:"security_group_ids"`
	PrivateIP        string            `json:"private_ip"`
	PublicIP         string            `json:"public_ip"`
	Status           string            `json:"status"`
	Tags             map[string]string `json:"tags"`
}

func NewInstanceDataSource() datasource.DataSource { return &InstanceDataSource{} }

func (d *InstanceDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_instance"
}

func (d *InstanceDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Fetches a CloudCore instance by ID or name.",
		Attributes: map[string]schema.Attribute{
			"id":                 schema.StringAttribute{Optional: true, Computed: true},
			"name":               schema.StringAttribute{Optional: true, Computed: true},
			"image_id":           schema.StringAttribute{Computed: true},
			"flavor":             schema.StringAttribute{Computed: true},
			"vpc_id":             schema.StringAttribute{Computed: true},
			"subnet_id":          schema.StringAttribute{Computed: true},
			"security_group_ids": schema.ListAttribute{Computed: true, ElementType: types.StringType},
			"private_ip":         schema.StringAttribute{Computed: true},
			"public_ip":          schema.StringAttribute{Computed: true},
			"status":             schema.StringAttribute{Computed: true},
			"tags":               schema.MapAttribute{Computed: true, ElementType: types.StringType},
		},
	}
}

func (d *InstanceDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("got %T", req.ProviderData))
		return
	}
	d.client = c
}

func (d *InstanceDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config InstanceDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if (config.ID.IsNull() || config.ID.ValueString() == "") && (config.Name.IsNull() || config.Name.ValueString() == "") {
		resp.Diagnostics.AddError("Missing lookup key", "At least one of 'id' or 'name' must be set.")
		return
	}

	set := func(i instanceItemAPI) {
		sgIDs, diags := types.ListValueFrom(ctx, types.StringType, i.SecurityGroupIDs)
		resp.Diagnostics.Append(diags...)
		tags, diags := types.MapValueFrom(ctx, types.StringType, i.Tags)
		resp.Diagnostics.Append(diags...)
		resp.Diagnostics.Append(resp.State.Set(ctx, &InstanceDataSourceModel{
			ID: types.StringValue(i.ID), Name: types.StringValue(i.Name),
			ImageID: types.StringValue(i.ImageID), Flavor: types.StringValue(i.Flavor),
			VPCID: types.StringValue(i.VPCID), SubnetID: types.StringValue(i.SubnetID),
			SecurityGroupIDs: sgIDs, PrivateIP: types.StringValue(i.PrivateIP),
			PublicIP: types.StringValue(i.PublicIP), Status: types.StringValue(i.Status), Tags: tags,
		})...)
	}

	if !config.ID.IsNull() && config.ID.ValueString() != "" {
		var result instanceItemAPI
		if err := d.client.Get(ctx, "/v1/instances/"+config.ID.ValueString(), &result); err != nil {
			resp.Diagnostics.AddError("Read instance failed", err.Error())
			return
		}
		set(result)
		return
	}

	var list struct {
		Items []instanceItemAPI `json:"items"`
	}
	if err := d.client.Get(ctx, "/v1/instances", &list); err != nil {
		resp.Diagnostics.AddError("List instances failed", err.Error())
		return
	}
	for _, i := range list.Items {
		if config.Name.IsNull() || i.Name == config.Name.ValueString() {
			set(i)
			return
		}
	}
	resp.Diagnostics.AddError("Instance not found", fmt.Sprintf("no instance matching id=%q name=%q",
		config.ID.ValueString(), config.Name.ValueString()))
}

// ── Load Balancer data source ─────────────────────────────────────────────────

var _ datasource.DataSource = &LoadBalancerDataSource{}

type LoadBalancerDataSource struct{ client *client.Client }

type LoadBalancerDataSourceModel struct {
	ID        types.String `tfsdk:"id"`
	Name      types.String `tfsdk:"name"`
	Type      types.String `tfsdk:"type"`
	VPCID     types.String `tfsdk:"vpc_id"`
	SubnetIDs types.List   `tfsdk:"subnet_ids"`
	Internal  types.Bool   `tfsdk:"internal"`
	DNSName   types.String `tfsdk:"dns_name"`
	Status    types.String `tfsdk:"status"`
	Tags      types.Map    `tfsdk:"tags"`
}

type lbItemAPI struct {
	ID        string            `json:"id"`
	Name      string            `json:"name"`
	Type      string            `json:"type"`
	VPCID     string            `json:"vpc_id"`
	SubnetIDs []string          `json:"subnet_ids"`
	Internal  bool              `json:"internal"`
	DNSName   string            `json:"dns_name"`
	Status    string            `json:"status"`
	Tags      map[string]string `json:"tags"`
}

func NewLoadBalancerDataSource() datasource.DataSource { return &LoadBalancerDataSource{} }

func (d *LoadBalancerDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_load_balancer"
}

func (d *LoadBalancerDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Fetches a CloudCore load balancer by ID or name.",
		Attributes: map[string]schema.Attribute{
			"id":         schema.StringAttribute{Optional: true, Computed: true},
			"name":       schema.StringAttribute{Optional: true, Computed: true},
			"type":       schema.StringAttribute{Computed: true},
			"vpc_id":     schema.StringAttribute{Computed: true},
			"subnet_ids": schema.ListAttribute{Computed: true, ElementType: types.StringType},
			"internal":   schema.BoolAttribute{Computed: true},
			"dns_name":   schema.StringAttribute{Computed: true},
			"status":     schema.StringAttribute{Computed: true},
			"tags":       schema.MapAttribute{Computed: true, ElementType: types.StringType},
		},
	}
}

func (d *LoadBalancerDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	c, ok := req.ProviderData.(*client.Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data type", fmt.Sprintf("got %T", req.ProviderData))
		return
	}
	d.client = c
}

func (d *LoadBalancerDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config LoadBalancerDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if (config.ID.IsNull() || config.ID.ValueString() == "") && (config.Name.IsNull() || config.Name.ValueString() == "") {
		resp.Diagnostics.AddError("Missing lookup key", "At least one of 'id' or 'name' must be set.")
		return
	}

	set := func(lb lbItemAPI) {
		subnetIDs, diags := types.ListValueFrom(ctx, types.StringType, lb.SubnetIDs)
		resp.Diagnostics.Append(diags...)
		tags, diags := types.MapValueFrom(ctx, types.StringType, lb.Tags)
		resp.Diagnostics.Append(diags...)
		resp.Diagnostics.Append(resp.State.Set(ctx, &LoadBalancerDataSourceModel{
			ID: types.StringValue(lb.ID), Name: types.StringValue(lb.Name),
			Type: types.StringValue(lb.Type), VPCID: types.StringValue(lb.VPCID),
			SubnetIDs: subnetIDs, Internal: types.BoolValue(lb.Internal),
			DNSName: types.StringValue(lb.DNSName), Status: types.StringValue(lb.Status), Tags: tags,
		})...)
	}

	if !config.ID.IsNull() && config.ID.ValueString() != "" {
		var result lbItemAPI
		if err := d.client.Get(ctx, "/v1/load-balancers/"+config.ID.ValueString(), &result); err != nil {
			resp.Diagnostics.AddError("Read load balancer failed", err.Error())
			return
		}
		set(result)
		return
	}

	var list struct {
		Items []lbItemAPI `json:"items"`
	}
	if err := d.client.Get(ctx, "/v1/load-balancers", &list); err != nil {
		resp.Diagnostics.AddError("List load balancers failed", err.Error())
		return
	}
	for _, lb := range list.Items {
		if config.Name.IsNull() || lb.Name == config.Name.ValueString() {
			set(lb)
			return
		}
	}
	resp.Diagnostics.AddError("Load balancer not found", fmt.Sprintf("no load balancer matching id=%q name=%q",
		config.ID.ValueString(), config.Name.ValueString()))
}
