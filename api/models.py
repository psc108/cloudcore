from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InstanceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    DELETED = "deleted"
    ERROR = "error"


class VPCStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class SubnetStatus(str, Enum):
    ACTIVE  = "active"
    DELETED = "deleted"


class IGWStatus(str, Enum):
    ACTIVE  = "active"
    DELETED = "deleted"


class RouteTableStatus(str, Enum):
    ACTIVE  = "active"
    DELETED = "deleted"


class LBStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class SecurityGroupStatus(str, Enum):
    ACTIVE  = "active"
    DELETED = "deleted"


class NfsServerStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    STOPPED  = "stopped"
    DELETED  = "deleted"
    ERROR    = "error"


@dataclass
class SecurityGroup:
    id: str = field(default_factory=new_id)
    name: str = ""
    description: str = ""
    vpc_id: str = ""
    ingress_rules: list = field(default_factory=list)
    egress_rules: list = field(default_factory=list)
    status: SecurityGroupStatus = SecurityGroupStatus.ACTIVE
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "vpc_id": self.vpc_id,
            "ingress_rules": self.ingress_rules,
            "egress_rules": self.egress_rules,
            "status": self.status.value,
            "created_at": self.created_at,
            "tags": self.tags,
        }


@dataclass
class VPC:
    id: str = field(default_factory=new_id)
    name: str = ""
    cidr_block: str = ""
    dns_support: bool = True
    status: VPCStatus = VPCStatus.ACTIVE
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "cidr_block": self.cidr_block,
            "dns_support": self.dns_support,
            "status": self.status.value,
            "created_at": self.created_at,
            "tags": self.tags,
        }


@dataclass
class Subnet:
    id: str = field(default_factory=new_id)
    name: str = ""
    vpc_id: str = ""
    cidr_block: str = ""
    public: bool = False
    zone: str = "a"
    status: SubnetStatus = SubnetStatus.ACTIVE
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "vpc_id": self.vpc_id,
            "cidr_block": self.cidr_block,
            "public": self.public,
            "zone": self.zone,
            "status": self.status.value,
            "created_at": self.created_at,
            "tags": self.tags,
        }


@dataclass
class InternetGateway:
    id: str = field(default_factory=new_id)
    name: str = ""
    vpc_id: str = ""
    status: IGWStatus = IGWStatus.ACTIVE
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "vpc_id": self.vpc_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "tags": self.tags,
        }


@dataclass
class RouteTable:
    id: str = field(default_factory=new_id)
    name: str = ""
    vpc_id: str = ""
    subnet_ids: list = field(default_factory=list)
    routes: list = field(default_factory=list)  # [{destination_cidr, gateway_id}]
    status: RouteTableStatus = RouteTableStatus.ACTIVE
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "vpc_id": self.vpc_id,
            "subnet_ids": self.subnet_ids,
            "routes": self.routes,
            "status": self.status.value,
            "created_at": self.created_at,
            "tags": self.tags,
        }


@dataclass
class Instance:
    id: str = field(default_factory=new_id)
    name: str = ""
    image_id: str = ""
    flavor: str = ""
    vpc_id: str = ""
    subnet_id: str = ""
    security_group_ids: list = field(default_factory=list)
    user_data: Optional[str] = None
    private_ip: str = ""
    public_ip: str = ""
    status: InstanceStatus = InstanceStatus.PENDING
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)
    # internal: libvirt domain name and host port forwards (SLIRP mode)
    domain_name: str = ""
    ssh_host_port: int = 0
    http_host_port: int = 0   # host port forwarded to guest :80; 0 = bridge mode
    ssh_user: str = "ubuntu"
    # [{username, sudo, ssh_keys: [], password_hash: ""}]
    users: list = field(default_factory=list)

    def to_dict(self) -> dict:
        # SLIRP instances are reachable at 127.0.0.1 via forwarded ports.
        effective_public_ip = self.public_ip or ("127.0.0.1" if self.ssh_host_port else "")
        ssh_endpoint = (
            f"{self.ssh_user}@127.0.0.1 -p {self.ssh_host_port}"
            if self.ssh_host_port else ""
        )
        return {
            "id": self.id,
            "name": self.name,
            "image_id": self.image_id,
            "flavor": self.flavor,
            "vpc_id": self.vpc_id,
            "subnet_id": self.subnet_id,
            "security_group_ids": self.security_group_ids,
            "private_ip": self.private_ip,
            "public_ip": effective_public_ip,
            "ssh_port": self.ssh_host_port,
            "ssh_user": self.ssh_user,
            "ssh_endpoint": ssh_endpoint,
            "users": self.users,
            "status": self.status.value,
            "created_at": self.created_at,
            "tags": self.tags,
        }


@dataclass
class NfsServer:
    id: str = field(default_factory=new_id)
    name: str = ""
    vpc_id: str = ""
    flavor: str = "standard.medium"
    disk_gb: int = 20
    status: NfsServerStatus = NfsServerStatus.PENDING
    private_ip: str = ""
    ssh_host_port: int = 0
    domain_name: str = ""
    shares: list = field(default_factory=list)  # [{name, clients, path}]
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "vpc_id": self.vpc_id,
            "flavor": self.flavor,
            "disk_gb": self.disk_gb,
            "status": self.status.value,
            "private_ip": self.private_ip,
            "ssh_port": self.ssh_host_port,
            "shares": self.shares,
            "created_at": self.created_at,
            "tags": self.tags,
        }


@dataclass
class LoadBalancer:
    id: str = field(default_factory=new_id)
    name: str = ""
    type: str = "application"
    vpc_id: str = ""
    subnet_ids: list = field(default_factory=list)
    internal: bool = False
    dns_name: str = ""
    listen_port: int = 0
    backends: list = field(default_factory=list)  # [{name, address, port}]
    # [{id, port, protocol, target_group_id, routing_rules, default_action}]
    listeners: list = field(default_factory=list)
    # {protocol, path, interval, healthy_threshold, unhealthy_threshold}
    health_check: dict = field(default_factory=dict)
    # [{id, name, port, protocol, targets, health_check, status}]
    target_groups: list = field(default_factory=list)
    sticky_sessions: bool = False
    cookie_name: str = "SERVERID"
    deletion_protection: bool = False
    status: LBStatus = LBStatus.ACTIVE
    created_at: str = field(default_factory=now_iso)
    tags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "vpc_id": self.vpc_id,
            "subnet_ids": self.subnet_ids,
            "internal": self.internal,
            "dns_name": self.dns_name,
            "listen_port": self.listen_port,
            "backends": self.backends,
            "listeners": self.listeners,
            "health_check": self.health_check,
            "target_groups": self.target_groups,
            "sticky_sessions": self.sticky_sessions,
            "cookie_name": self.cookie_name,
            "deletion_protection": self.deletion_protection,
            "status": self.status.value,
            "created_at": self.created_at,
            "tags": self.tags,
        }
