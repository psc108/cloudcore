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
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &InstanceResource{}
var _ resource.ResourceWithImportState = &InstanceResource{}

type InstanceResource struct {
	client *client.Client
}

type instanceUserModel struct {
	Username     types.String `tfsdk:"username"`
	Sudo         types.Bool   `tfsdk:"sudo"`
	SSHKeys      types.List   `tfsdk:"ssh_keys"`
	PasswordHash types.String `tfsdk:"password_hash"`
}

type InstanceResourceModel struct {
	ID               types.String   `tfsdk:"id"`
	Name             types.String   `tfsdk:"name"`
	ImageID          types.String   `tfsdk:"image_id"`
	Flavor           types.String   `tfsdk:"flavor"`
	VPCID            types.String   `tfsdk:"vpc_id"`
	SubnetID         types.String   `tfsdk:"subnet_id"`
	SecurityGroupIDs types.List     `tfsdk:"security_group_ids"`
	UsbDeviceIDs     types.List     `tfsdk:"usb_device_ids"`
	UserData         types.String   `tfsdk:"user_data"`
	Users            types.List     `tfsdk:"users"`
	PrivateIP        types.String   `tfsdk:"private_ip"`
	PublicIP         types.String   `tfsdk:"public_ip"`
	SSHPort          types.Int64    `tfsdk:"ssh_port"`
	SSHUser          types.String   `tfsdk:"ssh_user"`
	SSHEndpoint      types.String   `tfsdk:"ssh_endpoint"`
	Status           types.String   `tfsdk:"status"`
	CreatedAt        types.String   `tfsdk:"created_at"`
	Tags             types.Map      `tfsdk:"tags"`
	PeerID           types.String   `tfsdk:"peer_id"`
	HostHostname     types.String   `tfsdk:"host_hostname"`
	Timeouts         timeouts.Value `tfsdk:"timeouts"`
}

type instanceUserAPIModel struct {
	Username     string   `json:"username"`
	Sudo         bool     `json:"sudo"`
	SSHKeys      []string `json:"ssh_keys"`
	PasswordHash string   `json:"password_hash,omitempty"`
}

type instanceAPIModel struct {
	ID               string                 `json:"id"`
	Name             string                 `json:"name"`
	ImageID          string                 `json:"image_id"`
	Flavor           string                 `json:"flavor"`
	VPCID            string                 `json:"vpc_id"`
	SubnetID         string                 `json:"subnet_id"`
	SecurityGroupIDs []string               `json:"security_group_ids"`
	UsbDeviceIDs     []string               `json:"usb_device_ids"`
	UserData         string                 `json:"user_data,omitempty"`
	Users            []instanceUserAPIModel `json:"users,omitempty"`
	PrivateIP        string                 `json:"private_ip"`
	PublicIP         string                 `json:"public_ip"`
	SSHPort          int64                  `json:"ssh_port"`
	SSHUser          string                 `json:"ssh_user"`
	SSHEndpoint      string                 `json:"ssh_endpoint"`
	Status           string                 `json:"status"`
	CreatedAt        string                 `json:"created_at"`
	Tags             map[string]string      `json:"tags"`
	// PeerID is request-only (POST body field peer_id: which peer to
	// create this instance on). The API echoes the same concept back on
	// reads under a different key (host_id — which peer this instance
	// actually lives on), plus a convenience host_hostname — kept as
	// separate fields here rather than reusing PeerID for both
	// directions, since a plain field can't have two JSON tags.
	PeerID       string `json:"peer_id,omitempty"`
	HostID       string `json:"host_id,omitempty"`
	HostHostname string `json:"host_hostname,omitempty"`
}

func NewInstanceResource() resource.Resource { return &InstanceResource{} }

func (r *InstanceResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_instance"
}

func (r *InstanceResource) Schema(ctx context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		MarkdownDescription: "Manages a CloudCore compute instance (VM). Polls until `status = running` within the create timeout. API path: `/v1/instances`.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "API-assigned instance identifier.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"name":     schema.StringAttribute{Required: true, Description: "Instance name."},
			"image_id": schema.StringAttribute{Required: true, Description: "OS image identifier to boot from."},
			"flavor": schema.StringAttribute{
				Required:    true,
				Description: "Compute flavor: standard.nano, standard.small, standard.medium, standard.large, standard.xlarge, or standard.2xlarge.",
				Validators: []validator.String{
					stringvalidator.OneOf("standard.nano", "standard.small", "standard.medium", "standard.large", "standard.xlarge", "standard.2xlarge"),
				},
			},
			"vpc_id": schema.StringAttribute{
				Required:    true,
				Description: "VPC to attach the instance to. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"subnet_id": schema.StringAttribute{
				Required:    true,
				Description: "Subnet to place the instance in. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"security_group_ids": schema.ListAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "List of security group IDs to attach to the instance.",
			},
			"usb_device_ids": schema.ListAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "List of host USB device IDs (\"vendor_id:product_id\", from the cloudcore_usb_devices data source) to pass through to the instance. Mutable in place — devices are hot-attached/detached rather than requiring instance replacement. A device already attached to a different instance, or blocked (HID/hub/etc, see the data source), is rejected by the API.",
			},
			"user_data": schema.StringAttribute{
				Optional:    true,
				Sensitive:   true,
				Description: "Cloud-init user data script. Forces replacement on change.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"users": schema.ListNestedAttribute{
				Optional:    true,
				Description: "Extra users to create at boot via cloud-init, each with optional NOPASSWD sudo. The CloudCore inter-instance keypair is automatically added to every extra user's authorized_keys and installed in their ~/.ssh/ for outbound use, the same as the default image user. Baked into the initial cloud-init document, so forces replacement on change.",
				PlanModifiers: []planmodifier.List{
					listplanmodifier.RequiresReplace(),
				},
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"username": schema.StringAttribute{Required: true, Description: "Username to create."},
						"sudo": schema.BoolAttribute{
							Optional:    true,
							Description: "Grant passwordless (NOPASSWD) sudo. Defaults to false.",
						},
						"ssh_keys": schema.ListAttribute{
							Optional:    true,
							ElementType: types.StringType,
							Description: "Additional SSH public keys to authorize for this user, alongside the CloudCore keypair which is always added.",
						},
						"password_hash": schema.StringAttribute{
							Optional:    true,
							Sensitive:   true,
							Description: "Pre-hashed password (crypt format) for console/local login. Leave unset to lock password login.",
						},
					},
				},
			},
			"private_ip": schema.StringAttribute{Computed: true, Description: "Private IP address assigned by the API."},
			"public_ip":  schema.StringAttribute{Computed: true, Description: "Public IP address (127.0.0.1 for SLIRP instances)."},
			"ssh_port": schema.Int64Attribute{
				Computed:    true,
				Description: "Host port forwarded to the instance SSH service (SLIRP mode).",
				PlanModifiers: []planmodifier.Int64{
					int64planmodifier.UseStateForUnknown(),
				},
			},
			"ssh_user": schema.StringAttribute{
				Computed:    true,
				Description: "Default SSH username for the instance image.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"ssh_endpoint": schema.StringAttribute{
				Computed:    true,
				Description: "Ready-to-use SSH connection string, e.g. 'ubuntu@127.0.0.1 -p 22100'. Empty for bridge-networked instances.",
			},
			"status": schema.StringAttribute{Computed: true, Description: "Current instance status (API-assigned)."},
			"created_at": schema.StringAttribute{
				Computed:    true,
				Description: "ISO 8601 timestamp when the instance was created (API-assigned).",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"tags": schema.MapAttribute{
				Optional:    true,
				ElementType: types.StringType,
				Description: "Key/value tags to attach to the instance.",
			},
			"peer_id": schema.StringAttribute{
				Optional:    true,
				Description: "ID of a paired remote peer (see the cloudcore_peers data source) to create this instance on instead of the local host — for cross-host clustering. Must already be an approved pairing. Forces replacement on change, same as vpc_id/subnet_id: an existing instance can't be relocated to a different host.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"host_hostname": schema.StringAttribute{
				Computed:    true,
				Description: "Hostname of the physical host this instance actually lives on — the local host unless peer_id is set. Informational only, for plan/show output.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"timeouts": timeouts.Attributes(ctx, timeouts.Opts{
				Create: true,
				Delete: true,
			}),
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

func instanceUsersToAPI(ctx context.Context, list types.List) ([]instanceUserAPIModel, error) {
	if list.IsNull() || list.IsUnknown() {
		return nil, nil
	}
	var models []instanceUserModel
	if diags := list.ElementsAs(ctx, &models, false); diags.HasError() {
		return nil, fmt.Errorf("parsing users")
	}
	out := make([]instanceUserAPIModel, len(models))
	for i, m := range models {
		keys := []string{}
		if !m.SSHKeys.IsNull() && !m.SSHKeys.IsUnknown() {
			if diags := m.SSHKeys.ElementsAs(ctx, &keys, false); diags.HasError() {
				return nil, fmt.Errorf("parsing ssh_keys for user %q", m.Username.ValueString())
			}
		}
		out[i] = instanceUserAPIModel{
			Username:     m.Username.ValueString(),
			Sudo:         m.Sudo.ValueBool(),
			SSHKeys:      keys,
			PasswordHash: m.PasswordHash.ValueString(),
		}
	}
	return out, nil
}

// instanceUserObjectType is the nested object type of the `users`
// ListNestedAttribute (schema block above) — kept as a single source of
// truth so instanceUsersFromAPI's null and non-null branches always agree
// with each other and with the schema.
var instanceUserObjectType = types.ObjectType{AttrTypes: map[string]attr.Type{
	"username":      types.StringType,
	"sudo":          types.BoolType,
	"ssh_keys":      types.ListType{ElemType: types.StringType},
	"password_hash": types.StringType,
}}

// instanceUsersFromAPI converts the API's []instanceUserAPIModel into the
// `users` list's state representation — the reverse of instanceUsersToAPI
// below. Used ONLY by ImportState, deliberately NOT by instanceMapToState
// (see its own comment): Import starts from a genuinely blank state with
// no prior plan/config to inherit `users` from, so it's the one case where
// reconstructing from the API is actually correct. ImportState previously
// never set state.Users at all (left at its Go zero value), which the
// framework can't turn into a valid empty list on its own since a bare
// empty slice carries no element-type information — every import of a
// real instance with no extra `users` configured (the common case) failed
// outright before this existed.
func instanceUsersFromAPI(ctx context.Context, users []instanceUserAPIModel) (types.List, error) {
	values := make([]attr.Value, len(users))
	for i, u := range users {
		sshKeys, diags := stringsToList(ctx, u.SSHKeys)
		if diags.HasError() {
			return types.ListNull(instanceUserObjectType), fmt.Errorf("converting ssh_keys for user %q", u.Username)
		}
		passwordHash := types.StringNull()
		if u.PasswordHash != "" {
			passwordHash = types.StringValue(u.PasswordHash)
		}
		obj, diags := types.ObjectValue(instanceUserObjectType.AttrTypes, map[string]attr.Value{
			"username":      types.StringValue(u.Username),
			"sudo":          types.BoolValue(u.Sudo),
			"ssh_keys":      sshKeys,
			"password_hash": passwordHash,
		})
		if diags.HasError() {
			return types.ListNull(instanceUserObjectType), fmt.Errorf("building user object for %q", u.Username)
		}
		values[i] = obj
	}
	list, diags := objectsToList(instanceUserObjectType, values)
	if diags.HasError() {
		return types.ListNull(instanceUserObjectType), errors.New("building users list")
	}
	return list, nil
}

func instanceMapToState(ctx context.Context, result instanceAPIModel, state *InstanceResourceModel) error {
	state.ID = types.StringValue(result.ID)
	state.Name = types.StringValue(result.Name)
	state.ImageID = types.StringValue(result.ImageID)
	state.Flavor = types.StringValue(result.Flavor)
	state.VPCID = types.StringValue(result.VPCID)
	state.SubnetID = types.StringValue(result.SubnetID)
	state.PrivateIP = types.StringValue(result.PrivateIP)
	state.PublicIP = types.StringValue(result.PublicIP)
	state.SSHPort = types.Int64Value(result.SSHPort)
	state.SSHUser = types.StringValue(result.SSHUser)
	state.SSHEndpoint = types.StringValue(result.SSHEndpoint)
	state.Status = types.StringValue(result.Status)
	state.CreatedAt = types.StringValue(result.CreatedAt)
	sgIDs, diags := stringsToList(ctx, result.SecurityGroupIDs)
	if diags.HasError() {
		return fmt.Errorf("converting security_group_ids")
	}
	state.SecurityGroupIDs = sgIDs
	usbIDs, diags := stringsToList(ctx, result.UsbDeviceIDs)
	if diags.HasError() {
		return fmt.Errorf("converting usb_device_ids")
	}
	state.UsbDeviceIDs = usbIDs
	tags, diags := tagsToMap(ctx, result.Tags)
	if diags.HasError() {
		return fmt.Errorf("converting tags")
	}
	state.Tags = tags
	if result.HostID != "" {
		state.PeerID = types.StringValue(result.HostID)
	} else {
		state.PeerID = types.StringNull()
	}
	state.HostHostname = types.StringValue(result.HostHostname)
	// `users` is deliberately NOT set here. It's Optional but not Computed,
	// so Terraform's own protocol requires Create/Update's final state to
	// equal whatever was planned from config, byte for byte — the API
	// response can't distinguish "not configured" (null) from "configured
	// as an empty list" ([]), both come back as "users": [] from GET, so
	// reconstructing it here for Create/Update/Read would occasionally
	// collapse a real [] into null and trip "Provider produced
	// inconsistent result after apply" (found live: every module.workers/
	// module.coordinator instance sets users via a module default of [],
	// not by leaving it unconfigured). state already carries the correct
	// value from plan (Create/Update) or prior state (Read) — leave it
	// alone. ImportState is the one caller with no prior value to inherit
	// from; it sets state.Users itself via instanceUsersFromAPI below.
	return nil
}

func (r *InstanceResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan InstanceResourceModel
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

	sgIDs := []string{}
	resp.Diagnostics.Append(plan.SecurityGroupIDs.ElementsAs(ctx, &sgIDs, false)...)
	usbIDs := []string{}
	resp.Diagnostics.Append(plan.UsbDeviceIDs.ElementsAs(ctx, &usbIDs, false)...)
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)
	users, err := instanceUsersToAPI(ctx, plan.Users)
	if err != nil {
		resp.Diagnostics.AddError("Parse users failed", err.Error())
		return
	}

	body := instanceAPIModel{
		Name:             plan.Name.ValueString(),
		ImageID:          plan.ImageID.ValueString(),
		Flavor:           plan.Flavor.ValueString(),
		VPCID:            plan.VPCID.ValueString(),
		SubnetID:         plan.SubnetID.ValueString(),
		SecurityGroupIDs: sgIDs,
		UsbDeviceIDs:     usbIDs,
		UserData:         plan.UserData.ValueString(),
		Users:            users,
		Tags:             tags,
		PeerID:           plan.PeerID.ValueString(),
	}

	var result instanceAPIModel
	if err := r.client.Post(ctx, "/v1/instances", body, &result); err != nil {
		resp.Diagnostics.AddError("Create instance failed", err.Error())
		return
	}
	if err := instanceMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map instance state failed", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	// Poll until running or timeout.
	for {
		select {
		case <-ctx.Done():
			resp.Diagnostics.AddError(
				"Timeout waiting for instance",
				fmt.Sprintf("Instance %q did not reach 'running' within the create timeout. Last status: %s", result.ID, plan.Status.ValueString()),
			)
			return
		case <-time.After(10 * time.Second):
		}

		var poll instanceAPIModel
		if err := r.client.Get(ctx, "/v1/instances/"+result.ID, &poll); err != nil {
			resp.Diagnostics.AddError("Poll instance failed", err.Error())
			return
		}
		if err := instanceMapToState(ctx, poll, &plan); err != nil {
			resp.Diagnostics.AddError("Map instance state failed", err.Error())
			return
		}
		resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
		// Bridged instances report status=running before their DHCP lease
		// (and thus private_ip) is known — wait for both so this attribute
		// is reliable for cross-resource references (e.g. an NGINX
		// instance's user_data templating in a frontend instance's IP).
		if poll.Status == "running" && poll.PrivateIP != "" {
			return
		}
		if poll.Status == "error" {
			resp.Diagnostics.AddError("Instance entered error state", fmt.Sprintf("Instance %q status: error", result.ID))
			return
		}
	}
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
	if err := instanceMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map instance state failed", err.Error())
		return
	}
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
	usbIDs := []string{}
	resp.Diagnostics.Append(plan.UsbDeviceIDs.ElementsAs(ctx, &usbIDs, false)...)
	tags := map[string]string{}
	resp.Diagnostics.Append(plan.Tags.ElementsAs(ctx, &tags, false)...)

	body := instanceAPIModel{
		Name:             plan.Name.ValueString(),
		ImageID:          plan.ImageID.ValueString(),
		Flavor:           plan.Flavor.ValueString(),
		VPCID:            plan.VPCID.ValueString(),
		SubnetID:         plan.SubnetID.ValueString(),
		SecurityGroupIDs: sgIDs,
		UsbDeviceIDs:     usbIDs,
		Tags:             tags,
	}

	var result instanceAPIModel
	if err := r.client.Put(ctx, "/v1/instances/"+plan.ID.ValueString(), body, &result); err != nil {
		resp.Diagnostics.AddError("Update instance failed", err.Error())
		return
	}
	if err := instanceMapToState(ctx, result, &plan); err != nil {
		resp.Diagnostics.AddError("Map instance state failed", err.Error())
		return
	}
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
	var result instanceAPIModel
	if err := r.client.Get(ctx, "/v1/instances/"+req.ID, &result); err != nil {
		resp.Diagnostics.AddError("Import instance failed", err.Error())
		return
	}
	var state InstanceResourceModel
	state.Timeouts = timeouts.Value{
		Object: types.ObjectNull(map[string]attr.Type{
			"create": types.StringType,
			"delete": types.StringType,
		}),
	}
	if err := instanceMapToState(ctx, result, &state); err != nil {
		resp.Diagnostics.AddError("Map instance state failed", err.Error())
		return
	}
	usersList, err := instanceUsersFromAPI(ctx, result.Users)
	if err != nil {
		resp.Diagnostics.AddError("Map instance state failed", fmt.Sprintf("converting users: %s", err))
		return
	}
	state.Users = usersList
	resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
}
