package resources

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework-timeouts/resource/timeouts"
	"github.com/hashicorp/terraform-plugin-framework-validators/stringvalidator"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &NFSServerResource{}
var _ resource.ResourceWithImportState = &NFSServerResource{}

type NFSServerResource struct {
	client *client.Client
}

type nfsShareModel struct {
	Name    types.String `tfsdk:"name"`
	Clients types.String `tfsdk:"clients"`
	Path    types.String `tfsdk:"path"`
}

type NFSServerResourceModel struct {
	ID        types.String   `tfsdk:"id"`
	Name      types.String   `tfsdk:"name"`
	VPCID     types.String   `tfsdk:"vpc_id"`
	Flavor    types.String   `tfsdk:"flavor"`
	DiskGB    types.Int64    `tfsdk:"disk_gb"`
	Shares    types.List     `tfsdk:"shares"`
	PrivateIP types.String   `tfsdk:"private_ip"`
	Status    types.String   `tfsdk:"status"`
	Tags      types.Map      `tfsdk:"tags"`
	Timeouts  timeouts.Value `tfsdk:"timeouts"`
}

type nfsShareAPIModel struct {
	Name    string `json:"name"`
	Clients string `json:"clients,omitempty"`
	Path    string `json:"path,omitempty"`
}

type nfsServerAPIModel struct {
	ID        string             `json:"id"`
	Name      string             `json:"name"`
	VPCID     string             `json:"vpc_id"`
	Flavor    string             `json:"flavor"`
	DiskGB    int64              `json:"disk_gb"`
	Shares    []nfsShareAPIModel `json:"shares"`
	PrivateIP string             `json:"private_ip"`
	Status    string             `json:"status"`
	Tags      map[string]string  `json:"tags"`
}

var nfsShareAttrTypes = map[string]attr.Type{
	"name":    types.StringType,
	"clients": types.StringType,
	"path":    types.StringType,
}

func NewNFSServerResource() resource.Resource { return &NFSServerResource{} }

func (r *NFSServerResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_nfs_server"
}

func (r *NFSServerResource) Schema(ctx context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore NFS server with one or more exports. Polls until `status = running` within the create timeout. API path: `/v1/nfs-servers`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "API-assigned NFS server identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name": schema.StringAttribute{
				Required:    true,
				Description: "NFS server name. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"vpc_id": schema.StringAttribute{
				Required:    true,
				Description: "VPC ID the NFS server is attached to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"flavor": schema.StringAttribute{
				Optional:    true,
				Computed:    true,
				Description: "Compute flavor for the NFS server VM. Defaults to standard.medium.",
				Default:     stringdefault.StaticString("standard.medium"),
				Validators: []validator.String{
					stringvalidator.OneOf("standard.nano", "standard.small", "standard.medium", "standard.large"),
				},
			},
			"disk_gb": schema.Int64Attribute{
				Optional:    true,
				Computed:    true,
				Description: "Storage disk size in GiB. Defaults to 20.",
				Default:     int64default.StaticInt64(20),
			},
			"shares": schema.ListNestedAttribute{
				Optional:    true,
				Description: "NFS exports to create on this server.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"name": schema.StringAttribute{
							Required:    true,
							Description: "Export name (becomes /exports/<name>).",
						},
						"clients": schema.StringAttribute{
							Optional:    true,
							Computed:    true,
							Description: "Client access spec. Defaults to 'vpc' (all VPC hosts).",
							Default:     stringdefault.StaticString("vpc"),
						},
						"path": schema.StringAttribute{
							Computed:    true,
							Description: "Resolved export path on the server.",
							PlanModifiers: []planmodifier.String{
								stringplanmodifier.UseStateForUnknown(),
							},
						},
					},
				},
			},
			"private_ip": schema.StringAttribute{
				Computed:    true,
				Description: "Private IP address assigned to the NFS server.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"status": schema.StringAttribute{
				Computed:    true,
				Description: "Current status of the NFS server.",
			},
			"tags": schema.MapAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "Key/value tags to attach to the NFS server.",
			},
			"timeouts": timeouts.Attributes(ctx, timeouts.Opts{
				Create: true,
				Delete: true,
			}),
		},
	}
}

func (r *NFSServerResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func (r *NFSServerResource) sharesFromAPI(ctx context.Context, apiShares []nfsShareAPIModel) (types.List, error) {
	elems := make([]attr.Value, len(apiShares))
	for i, s := range apiShares {
		obj, diags := types.ObjectValue(nfsShareAttrTypes, map[string]attr.Value{
			"name":    types.StringValue(s.Name),
			"clients": types.StringValue(s.Clients),
			"path":    types.StringValue(s.Path),
		})
		if diags.HasError() {
			return types.ListNull(types.ObjectType{AttrTypes: nfsShareAttrTypes}), fmt.Errorf("building share object")
		}
		elems[i] = obj
	}
	list, diags := types.ListValue(types.ObjectType{AttrTypes: nfsShareAttrTypes}, elems)
	if diags.HasError() {
		return types.ListNull(types.ObjectType{AttrTypes: nfsShareAttrTypes}), fmt.Errorf("building shares list")
	}
	return list, nil
}

func (r *NFSServerResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan NFSServerResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	createTimeout, diags := plan.Timeouts.Create(ctx, 10*time.Minute)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, createTimeout)
	defer cancel()

	var planShares []nfsShareModel
	resp.Diagnostics.Append(plan.Shares.ElementsAs(ctx, &planShares, false)...)
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	apiShares := make([]nfsShareAPIModel, len(planShares))
	for i, s := range planShares {
		apiShares[i] = nfsShareAPIModel{Name: s.Name.ValueString(), Clients: s.Clients.ValueString()}
	}

	body := nfsServerAPIModel{
		Name:   plan.Name.ValueString(),
		VPCID:  plan.VPCID.ValueString(),
		Flavor: plan.Flavor.ValueString(),
		DiskGB: plan.DiskGB.ValueInt64(),
		Shares: apiShares,
		Tags:   tags,
	}

	var result nfsServerAPIModel
	if err := r.client.Post(ctx, "/v1/nfs-servers", body, &result); err != nil {
		resp.Diagnostics.AddError("Create NFS server failed", err.Error())
		return
	}

	plan.ID = types.StringValue(result.ID)
	plan.PrivateIP = types.StringValue(result.PrivateIP)
	plan.Status = types.StringValue(result.Status)

	shares, err := r.sharesFromAPI(ctx, result.Shares)
	if err != nil {
		resp.Diagnostics.AddError("Parse NFS shares failed", err.Error())
		return
	}
	plan.Shares = shares
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Poll until running or timeout.
	for {
		select {
		case <-ctx.Done():
			resp.Diagnostics.AddError(
				"Timeout waiting for NFS server",
				fmt.Sprintf("NFS server %q did not reach 'running' within the create timeout. Last status: %s", result.ID, plan.Status.ValueString()),
			)
			return
		case <-time.After(10 * time.Second):
		}
		var poll nfsServerAPIModel
		if err := r.client.Get(ctx, "/v1/nfs-servers/"+result.ID, &poll); err != nil {
			resp.Diagnostics.AddError("Poll NFS server failed", err.Error())
			return
		}
		plan.Status = types.StringValue(poll.Status)
		plan.PrivateIP = types.StringValue(poll.PrivateIP)
		pollShares, err := r.sharesFromAPI(ctx, poll.Shares)
		if err != nil {
			resp.Diagnostics.AddError("Parse NFS shares failed", err.Error())
			return
		}
		plan.Shares = pollShares
		resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
		if poll.Status == "running" {
			return
		}
		if poll.Status == "error" {
			resp.Diagnostics.AddError("NFS server entered error state", fmt.Sprintf("NFS server %q status: error", result.ID))
			return
		}
	}
}

func (r *NFSServerResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state NFSServerResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result nfsServerAPIModel
	if err := r.client.Get(ctx, "/v1/nfs-servers/"+state.ID.ValueString(), &result); err != nil {
		var nfe *client.NotFoundError
		if errors.As(err, &nfe) {
			resp.State.RemoveResource(ctx)
			return
		}
		resp.Diagnostics.AddError("Read NFS server failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.VPCID = types.StringValue(result.VPCID)
	state.Flavor = types.StringValue(result.Flavor)
	state.DiskGB = types.Int64Value(result.DiskGB)
	state.PrivateIP = types.StringValue(result.PrivateIP)
	state.Status = types.StringValue(result.Status)

	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags

	shares, err := r.sharesFromAPI(ctx, result.Shares)
	if err != nil {
		resp.Diagnostics.AddError("Parse NFS shares failed", err.Error())
		return
	}
	state.Shares = shares
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}

func (r *NFSServerResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan NFSServerResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	var state NFSServerResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var planShares []nfsShareModel
	resp.Diagnostics.Append(plan.Shares.ElementsAs(ctx, &planShares, false)...)
	var stateShares []nfsShareModel
	resp.Diagnostics.Append(state.Shares.ElementsAs(ctx, &stateShares, false)...)

	stateNames := map[string]bool{}
	for _, s := range stateShares {
		stateNames[s.Name.ValueString()] = true
	}
	planNames := map[string]bool{}
	for _, s := range planShares {
		planNames[s.Name.ValueString()] = true
	}

	nfsID := state.ID.ValueString()

	for _, s := range planShares {
		if !stateNames[s.Name.ValueString()] {
			body := map[string]string{"name": s.Name.ValueString(), "clients": s.Clients.ValueString()}
			if err := r.client.Post(ctx, "/v1/nfs-servers/"+nfsID+"/shares", body, nil); err != nil {
				resp.Diagnostics.AddError("Add NFS share failed", err.Error())
			}
		}
	}
	for _, s := range stateShares {
		if !planNames[s.Name.ValueString()] {
			path := fmt.Sprintf("/v1/nfs-servers/%s/shares/%s", nfsID, s.Name.ValueString())
			if err := r.client.Delete(ctx, path); err != nil {
				resp.Diagnostics.AddError("Remove NFS share failed", err.Error())
			}
		}
	}

	// Re-read always — reflects actual server state even after partial failure.
	var result nfsServerAPIModel
	if err := r.client.Get(ctx, "/v1/nfs-servers/"+nfsID, &result); err != nil {
		resp.Diagnostics.AddError("Read NFS server after update failed", err.Error())
		return
	}
	plan.ID = state.ID
	plan.PrivateIP = types.StringValue(result.PrivateIP)
	plan.Status = types.StringValue(result.Status)
	shares, err := r.sharesFromAPI(ctx, result.Shares)
	if err != nil {
		resp.Diagnostics.AddError("Parse NFS shares failed", err.Error())
		return
	}
	plan.Shares = shares
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *NFSServerResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state NFSServerResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, "/v1/nfs-servers/"+state.ID.ValueString()); err != nil {
		resp.Diagnostics.AddError("Delete NFS server failed", err.Error())
	}
}

func (r *NFSServerResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	var state NFSServerResourceModel
	state.ID = types.StringValue(req.ID)

	var result nfsServerAPIModel
	if err := r.client.Get(ctx, "/v1/nfs-servers/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import NFS server failed", err.Error())
		return
	}

	state.Name = types.StringValue(result.Name)
	state.VPCID = types.StringValue(result.VPCID)
	state.Flavor = types.StringValue(result.Flavor)
	state.DiskGB = types.Int64Value(result.DiskGB)
	state.PrivateIP = types.StringValue(result.PrivateIP)
	state.Status = types.StringValue(result.Status)
	state.Timeouts = timeouts.Value{}

	tags, diags := types.MapValueFrom(ctx, types.StringType, result.Tags)
	resp.Diagnostics.Append(diags...)
	state.Tags = tags

	shares, err := r.sharesFromAPI(ctx, result.Shares)
	if err != nil {
		resp.Diagnostics.AddError("Parse NFS shares failed", err.Error())
		return
	}
	state.Shares = shares
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
