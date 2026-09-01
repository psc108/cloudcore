package datasources

import (
	"context"
	"fmt"
	"strings"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ── VPC data source ──────────────────────────────────────────────────────
// Note: all list endpoints return the full result set in a single response
// (no pagination). If the API adds pagination in future, the Read methods
// below must be updated to follow next-page links before filtering.────

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

// ── Security Group data source ────────────────────────────────────────────────

var _ datasource.DataSource = &SecurityGroupDataSource{}

type SecurityGroupDataSource struct{ client *client.Client }

type SecurityGroupDataSourceModel struct {
	ID          types.String `tfsdk:"id"`
	Name        types.String `tfsdk:"name"`
	Description types.String `tfsdk:"description"`
	VPCID       types.String `tfsdk:"vpc_id"`
	Status      types.String `tfsdk:"status"`
	CreatedAt   types.String `tfsdk:"created_at"`
	Tags        types.Map    `tfsdk:"tags"`
}

type sgItemAPI struct {
	ID          string            `json:"id"`
	Name        string            `json:"name"`
	Description string            `json:"description"`
	VPCID       string            `json:"vpc_id"`
	Status      string            `json:"status"`
	CreatedAt   string            `json:"created_at"`
	Tags        map[string]string `json:"tags"`
}

func NewSecurityGroupDataSource() datasource.DataSource { return &SecurityGroupDataSource{} }

func (d *SecurityGroupDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_security_group"
}

func (d *SecurityGroupDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Fetches a CloudCore security group by ID or name.",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Optional: true, Computed: true},
			"name":        schema.StringAttribute{Optional: true, Computed: true},
			"description": schema.StringAttribute{Computed: true},
			"vpc_id":      schema.StringAttribute{Computed: true},
			"status":      schema.StringAttribute{Computed: true},
			"created_at":  schema.StringAttribute{Computed: true},
			"tags":        schema.MapAttribute{Computed: true, ElementType: types.StringType},
		},
	}
}

func (d *SecurityGroupDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *SecurityGroupDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config SecurityGroupDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if (config.ID.IsNull() || config.ID.ValueString() == "") && (config.Name.IsNull() || config.Name.ValueString() == "") {
		resp.Diagnostics.AddError("Missing lookup key", "At least one of 'id' or 'name' must be set.")
		return
	}

	set := func(sg sgItemAPI) {
		tags, diags := types.MapValueFrom(ctx, types.StringType, sg.Tags)
		resp.Diagnostics.Append(diags...)
		resp.Diagnostics.Append(resp.State.Set(ctx, &SecurityGroupDataSourceModel{
			ID: types.StringValue(sg.ID), Name: types.StringValue(sg.Name),
			Description: types.StringValue(sg.Description), VPCID: types.StringValue(sg.VPCID),
			Status: types.StringValue(sg.Status), CreatedAt: types.StringValue(sg.CreatedAt), Tags: tags,
		})...)
	}

	if !config.ID.IsNull() && config.ID.ValueString() != "" {
		var result sgItemAPI
		if err := d.client.Get(ctx, "/v1/security-groups/"+config.ID.ValueString(), &result); err != nil {
			resp.Diagnostics.AddError("Read security group failed", err.Error())
			return
		}
		set(result)
		return
	}

	var list struct {
		Items []sgItemAPI `json:"items"`
	}
	if err := d.client.Get(ctx, "/v1/security-groups", &list); err != nil {
		resp.Diagnostics.AddError("List security groups failed", err.Error())
		return
	}
	for _, sg := range list.Items {
		if sg.Name == config.Name.ValueString() {
			set(sg)
			return
		}
	}
	resp.Diagnostics.AddError("Security group not found", fmt.Sprintf("no security group matching id=%q name=%q",
		config.ID.ValueString(), config.Name.ValueString()))
}

// ── NFS Server data source ────────────────────────────────────────────────────

var _ datasource.DataSource = &NFSServerDataSource{}

type NFSServerDataSource struct{ client *client.Client }

type NFSServerDataSourceModel struct {
	ID        types.String `tfsdk:"id"`
	Name      types.String `tfsdk:"name"`
	VPCID     types.String `tfsdk:"vpc_id"`
	Flavor    types.String `tfsdk:"flavor"`
	DiskGB    types.Int64  `tfsdk:"disk_gb"`
	PrivateIP types.String `tfsdk:"private_ip"`
	Status    types.String `tfsdk:"status"`
	Tags      types.Map    `tfsdk:"tags"`
}

type nfsItemAPI struct {
	ID        string            `json:"id"`
	Name      string            `json:"name"`
	VPCID     string            `json:"vpc_id"`
	Flavor    string            `json:"flavor"`
	DiskGB    int64             `json:"disk_gb"`
	PrivateIP string            `json:"private_ip"`
	Status    string            `json:"status"`
	Tags      map[string]string `json:"tags"`
}

func NewNFSServerDataSource() datasource.DataSource { return &NFSServerDataSource{} }

func (d *NFSServerDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_nfs_server"
}

func (d *NFSServerDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Fetches a CloudCore NFS server by ID or name.",
		Attributes: map[string]schema.Attribute{
			"id":         schema.StringAttribute{Optional: true, Computed: true},
			"name":       schema.StringAttribute{Optional: true, Computed: true},
			"vpc_id":     schema.StringAttribute{Computed: true},
			"flavor":     schema.StringAttribute{Computed: true},
			"disk_gb":    schema.Int64Attribute{Computed: true},
			"private_ip": schema.StringAttribute{Computed: true},
			"status":     schema.StringAttribute{Computed: true},
			"tags":       schema.MapAttribute{Computed: true, ElementType: types.StringType},
		},
	}
}

func (d *NFSServerDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *NFSServerDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config NFSServerDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if (config.ID.IsNull() || config.ID.ValueString() == "") && (config.Name.IsNull() || config.Name.ValueString() == "") {
		resp.Diagnostics.AddError("Missing lookup key", "At least one of 'id' or 'name' must be set.")
		return
	}

	set := func(n nfsItemAPI) {
		tags, diags := types.MapValueFrom(ctx, types.StringType, n.Tags)
		resp.Diagnostics.Append(diags...)
		resp.Diagnostics.Append(resp.State.Set(ctx, &NFSServerDataSourceModel{
			ID: types.StringValue(n.ID), Name: types.StringValue(n.Name),
			VPCID: types.StringValue(n.VPCID), Flavor: types.StringValue(n.Flavor),
			DiskGB: types.Int64Value(n.DiskGB), PrivateIP: types.StringValue(n.PrivateIP),
			Status: types.StringValue(n.Status), Tags: tags,
		})...)
	}

	if !config.ID.IsNull() && config.ID.ValueString() != "" {
		var result nfsItemAPI
		if err := d.client.Get(ctx, "/v1/nfs-servers/"+config.ID.ValueString(), &result); err != nil {
			resp.Diagnostics.AddError("Read NFS server failed", err.Error())
			return
		}
		set(result)
		return
	}

	var list struct {
		Items []nfsItemAPI `json:"items"`
	}
	if err := d.client.Get(ctx, "/v1/nfs-servers", &list); err != nil {
		resp.Diagnostics.AddError("List NFS servers failed", err.Error())
		return
	}
	for _, n := range list.Items {
		if n.Name == config.Name.ValueString() {
			set(n)
			return
		}
	}
	resp.Diagnostics.AddError("NFS server not found", fmt.Sprintf("no NFS server matching id=%q name=%q",
		config.ID.ValueString(), config.Name.ValueString()))
}

// ── DNS Zone data source ──────────────────────────────────────────────────────

var _ datasource.DataSource = &DNSZoneDataSource{}

type DNSZoneDataSource struct{ client *client.Client }

type DNSZoneDataSourceModel struct {
	Name      types.String `tfsdk:"name"`
	CreatedAt types.String `tfsdk:"created_at"`
}

type dnsZoneItemAPI struct {
	Name      string `json:"name"`
	CreatedAt string `json:"created_at"`
}

func NewDNSZoneDataSource() datasource.DataSource { return &DNSZoneDataSource{} }

func (d *DNSZoneDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_dns_zone"
}

func (d *DNSZoneDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Fetches a CloudCore DNS zone by name.",
		Attributes: map[string]schema.Attribute{
			"name":       schema.StringAttribute{Required: true},
			"created_at": schema.StringAttribute{Computed: true},
		},
	}
}

func (d *DNSZoneDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *DNSZoneDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config DNSZoneDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var list struct {
		Items []dnsZoneItemAPI `json:"items"`
	}
	if err := d.client.Get(ctx, "/v1/dns/zones", &list); err != nil {
		resp.Diagnostics.AddError("List DNS zones failed", err.Error())
		return
	}
	for _, z := range list.Items {
		if z.Name == config.Name.ValueString() {
			resp.Diagnostics.Append(resp.State.Set(ctx, &DNSZoneDataSourceModel{
				Name:      types.StringValue(z.Name),
				CreatedAt: types.StringValue(z.CreatedAt),
			})...)
			return
		}
	}
	resp.Diagnostics.AddError("DNS zone not found", fmt.Sprintf("no zone named %q", config.Name.ValueString()))
}

// ── DNS Record data source ────────────────────────────────────────────────────

var _ datasource.DataSource = &DNSRecordDataSource{}

type DNSRecordDataSource struct{ client *client.Client }

type DNSRecordDataSourceModel struct {
	Zone  types.String `tfsdk:"zone"`
	Name  types.String `tfsdk:"name"`
	Type  types.String `tfsdk:"type"`
	Value types.String `tfsdk:"value"`
	TTL   types.Int64  `tfsdk:"ttl"`
}

type dnsRecordItemAPI struct {
	Name  string `json:"name"`
	Type  string `json:"type"`
	Value string `json:"value"`
	TTL   int64  `json:"ttl"`
}

func NewDNSRecordDataSource() datasource.DataSource { return &DNSRecordDataSource{} }

func (d *DNSRecordDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_dns_record"
}

func (d *DNSRecordDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Fetches a CloudCore DNS record by zone, name, and type.",
		Attributes: map[string]schema.Attribute{
			"zone":  schema.StringAttribute{Required: true},
			"name":  schema.StringAttribute{Required: true},
			"type":  schema.StringAttribute{Required: true},
			"value": schema.StringAttribute{Computed: true},
			"ttl":   schema.Int64Attribute{Computed: true},
		},
	}
}

func (d *DNSRecordDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
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

func (d *DNSRecordDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var config DNSRecordDataSourceModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &config)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var list struct {
		Items []dnsRecordItemAPI `json:"items"`
	}
	if err := d.client.Get(ctx, "/v1/dns/zones/"+config.Zone.ValueString()+"/records", &list); err != nil {
		resp.Diagnostics.AddError("List DNS records failed", err.Error())
		return
	}
	for _, rec := range list.Items {
		if rec.Name == config.Name.ValueString() && strings.EqualFold(rec.Type, config.Type.ValueString()) {
			resp.Diagnostics.Append(resp.State.Set(ctx, &DNSRecordDataSourceModel{
				Zone:  config.Zone,
				Name:  types.StringValue(rec.Name),
				Type:  types.StringValue(strings.ToUpper(rec.Type)),
				Value: types.StringValue(rec.Value),
				TTL:   types.Int64Value(rec.TTL),
			})...)
			return
		}
	}
	resp.Diagnostics.AddError("DNS record not found", fmt.Sprintf("no %s record named %q in zone %q",
		config.Type.ValueString(), config.Name.ValueString(), config.Zone.ValueString()))
}
