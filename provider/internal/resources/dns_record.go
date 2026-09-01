package resources

import (
	"context"
	"fmt"
	"strings"

	"github.com/cloudcore/terraform-provider-cloudcore/internal/client"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

var _ resource.Resource = &DNSRecordResource{}
var _ resource.ResourceWithImportState = &DNSRecordResource{}

type DNSRecordResource struct {
	client *client.Client
}

type DNSRecordResourceModel struct {
	ID    types.String `tfsdk:"id"`
	Zone  types.String `tfsdk:"zone"`
	Name  types.String `tfsdk:"name"`
	Type  types.String `tfsdk:"type"`
	Value types.String `tfsdk:"value"`
	TTL   types.Int64  `tfsdk:"ttl"`
}

type dnsRecordAPIModel struct {
	Name  string `json:"name"`
	Type  string `json:"type"`
	Value string `json:"value"`
	TTL   int64  `json:"ttl"`
}

type dnsRecordListAPIModel struct {
	Items []dnsRecordAPIModel `json:"items"`
}

func NewDNSRecordResource() resource.Resource { return &DNSRecordResource{} }

func (r *DNSRecordResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_dns_record"
}

func (r *DNSRecordResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages a CloudCore DNS record within a zone.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{
				Computed:    true,
				Description: "Record identifier in the form zone/name/type.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.UseStateForUnknown(),
				},
			},
			"zone": schema.StringAttribute{
				Required:    true,
				Description: "Name of the DNS zone this record belongs to.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"name": schema.StringAttribute{
				Required:    true,
				Description: "Record name (e.g. 'www', '@').",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"type": schema.StringAttribute{
				Required:    true,
				Description: "Record type: A, CNAME, or TXT.",
				PlanModifiers: []planmodifier.String{
					stringplanmodifier.RequiresReplace(),
				},
			},
			"value": schema.StringAttribute{
				Required:    true,
				Description: "Record value (IP address, hostname, or text).",
			},
			"ttl": schema.Int64Attribute{
				Optional:    true,
				Computed:    true,
				Description: "Time-to-live in seconds. Defaults to 300.",
				Default:     int64default.StaticInt64(300),
			},
		},
	}
}

func (r *DNSRecordResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
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

func recordID(zone, name, rtype string) string {
	return fmt.Sprintf("%s/%s/%s", zone, name, strings.ToUpper(rtype))
}

func (r *DNSRecordResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var plan DNSRecordResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body := map[string]any{
		"name":  plan.Name.ValueString(),
		"type":  plan.Type.ValueString(),
		"value": plan.Value.ValueString(),
		"ttl":   plan.TTL.ValueInt64(),
	}

	var result dnsRecordAPIModel
	if err := r.client.Post(ctx, "/v1/dns/zones/"+plan.Zone.ValueString()+"/records", body, &result); err != nil {
		resp.Diagnostics.AddError("Create DNS record failed", err.Error())
		return
	}

	plan.ID = types.StringValue(recordID(plan.Zone.ValueString(), plan.Name.ValueString(), plan.Type.ValueString()))
	plan.TTL = types.Int64Value(result.TTL)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *DNSRecordResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var state DNSRecordResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}

	var result dnsRecordListAPIModel
	if err := r.client.Get(ctx, "/v1/dns/zones/"+state.Zone.ValueString()+"/records", &result); err != nil {
		resp.Diagnostics.AddError("Read DNS records failed", err.Error())
		return
	}
	for _, rec := range result.Items {
		if rec.Name == state.Name.ValueString() && strings.EqualFold(rec.Type, state.Type.ValueString()) {
			state.ID = types.StringValue(recordID(state.Zone.ValueString(), rec.Name, rec.Type))
			state.Value = types.StringValue(rec.Value)
			state.TTL = types.Int64Value(rec.TTL)
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.State.RemoveResource(ctx)
}

func (r *DNSRecordResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var plan DNSRecordResourceModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &plan)...)
	if resp.Diagnostics.HasError() {
		return
	}

	body := map[string]any{
		"name":  plan.Name.ValueString(),
		"type":  plan.Type.ValueString(),
		"value": plan.Value.ValueString(),
		"ttl":   plan.TTL.ValueInt64(),
	}
	var result dnsRecordAPIModel
	if err := r.client.Post(ctx, "/v1/dns/zones/"+plan.Zone.ValueString()+"/records", body, &result); err != nil {
		resp.Diagnostics.AddError("Update DNS record failed", err.Error())
		return
	}
	plan.ID = types.StringValue(recordID(plan.Zone.ValueString(), plan.Name.ValueString(), plan.Type.ValueString()))
	plan.TTL = types.Int64Value(result.TTL)
	resp.Diagnostics.Append(resp.State.Set(ctx, &plan)...)
}

func (r *DNSRecordResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var state DNSRecordResourceModel
	resp.Diagnostics.Append(req.State.Get(ctx, &state)...)
	if resp.Diagnostics.HasError() {
		return
	}
	path := fmt.Sprintf("/v1/dns/zones/%s/records/%s/%s",
		state.Zone.ValueString(), state.Name.ValueString(), state.Type.ValueString())
	if err := r.client.Delete(ctx, path); err != nil {
		resp.Diagnostics.AddError("Delete DNS record failed", err.Error())
	}
}

// ImportState accepts "zone/name/type" e.g. "myapp.local/www/A"
func (r *DNSRecordResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	parts := strings.SplitN(req.ID, "/", 3)
	if len(parts) != 3 {
		resp.Diagnostics.AddError("Invalid import ID", "Expected format: zone/name/type (e.g. myapp.local/www/A)")
		return
	}
	zone, name, rtype := parts[0], parts[1], strings.ToUpper(parts[2])

	var result dnsRecordListAPIModel
	if err := r.client.Get(ctx, "/v1/dns/zones/"+zone+"/records", &result); err != nil {
		resp.Diagnostics.AddError("Import DNS record failed", err.Error())
		return
	}
	for _, rec := range result.Items {
		if rec.Name == name && strings.EqualFold(rec.Type, rtype) {
			state := DNSRecordResourceModel{
				ID:    types.StringValue(recordID(zone, name, rtype)),
				Zone:  types.StringValue(zone),
				Name:  types.StringValue(rec.Name),
				Type:  types.StringValue(strings.ToUpper(rec.Type)),
				Value: types.StringValue(rec.Value),
				TTL:   types.Int64Value(rec.TTL),
			}
			resp.Diagnostics.Append(resp.State.Set(ctx, &state)...)
			return
		}
	}
	resp.Diagnostics.AddError("Import DNS record failed", fmt.Sprintf("record '%s/%s' not found in zone '%s'", name, rtype, zone))
}
