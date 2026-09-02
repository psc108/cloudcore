from __future__ import annotations

import json
from typing import Optional

import db
from models import (
    VPC, Instance, LoadBalancer, InstanceStatus, VPCStatus, LBStatus,
    Subnet, SubnetStatus, InternetGateway, IGWStatus, RouteTable, RouteTableStatus,
)


def _vpc_from_row(row) -> VPC:
    v = VPC(
        id=row["id"], name=row["name"], cidr_block=row["cidr_block"],
        dns_support=bool(row["dns_support"]), created_at=row["created_at"],
        tags=json.loads(row["tags"]),
    )
    v.status = VPCStatus(row["status"])
    return v


def _inst_from_row(row) -> Instance:
    keys = row.keys()
    i = Instance(
        id=row["id"], name=row["name"], image_id=row["image_id"],
        flavor=row["flavor"], vpc_id=row["vpc_id"], subnet_id=row["subnet_id"],
        security_group_ids=json.loads(row["security_group_ids"]),
        user_data=row["user_data"], private_ip=row["private_ip"],
        public_ip=row["public_ip"], created_at=row["created_at"],
        tags=json.loads(row["tags"]), domain_name=row["domain_name"],
        ssh_host_port=row["ssh_host_port"],
        http_host_port=row["http_host_port"] if "http_host_port" in keys else 0,
        ssh_user=row["ssh_user"],
        users=json.loads(row["users"]),
    )
    i.status = InstanceStatus(row["status"])
    return i


def _lb_from_row(row) -> LoadBalancer:
    lb = LoadBalancer(
        id=row["id"], name=row["name"], type=row["type"], vpc_id=row["vpc_id"],
        subnet_ids=json.loads(row["subnet_ids"]), internal=bool(row["internal"]),
        dns_name=row["dns_name"], listen_port=row["listen_port"],
        backends=json.loads(row["backends"]),
        listeners=json.loads(row["listeners"]) if row["listeners"] else [],
        health_check=json.loads(row["health_check"]) if row["health_check"] else {},
        created_at=row["created_at"],
        tags=json.loads(row["tags"]),
    )
    lb.status = LBStatus(row["status"])
    return lb


def _subnet_from_row(row) -> Subnet:
    s = Subnet(
        id=row["id"], name=row["name"], vpc_id=row["vpc_id"],
        cidr_block=row["cidr_block"], public=bool(row["public"]),
        zone=row["zone"], created_at=row["created_at"],
        tags=json.loads(row["tags"]),
    )
    s.status = SubnetStatus(row["status"])
    return s


def _igw_from_row(row) -> InternetGateway:
    g = InternetGateway(
        id=row["id"], name=row["name"], vpc_id=row["vpc_id"],
        created_at=row["created_at"], tags=json.loads(row["tags"]),
    )
    g.status = IGWStatus(row["status"])
    return g


def _rt_from_row(row) -> RouteTable:
    rt = RouteTable(
        id=row["id"], name=row["name"], vpc_id=row["vpc_id"],
        subnet_ids=json.loads(row["subnet_ids"]),
        routes=json.loads(row["routes"]),
        created_at=row["created_at"], tags=json.loads(row["tags"]),
    )
    rt.status = RouteTableStatus(row["status"])
    return rt


# Keep load() as a no-op — db.init() is called from server.py instead
def load() -> None:
    pass


# --- VPC ---

def list_vpcs() -> list[VPC]:
    rows = db.get_db().execute(
        "SELECT * FROM vpcs WHERE status != 'deleted'").fetchall()
    return [_vpc_from_row(r) for r in rows]


def get_vpc(vpc_id: str) -> Optional[VPC]:
    row = db.get_db().execute(
        "SELECT * FROM vpcs WHERE id=? AND status != 'deleted'", (vpc_id,)).fetchone()
    return _vpc_from_row(row) if row else None


def find_vpc_by_name(name: str) -> Optional[VPC]:
    row = db.get_db().execute(
        "SELECT * FROM vpcs WHERE name=? AND status != 'deleted'", (name,)).fetchone()
    return _vpc_from_row(row) if row else None


def put_vpc(vpc: VPC) -> None:
    db.get_db().execute("""INSERT INTO vpcs (id,name,cidr_block,dns_support,status,created_at,tags)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, cidr_block=excluded.cidr_block,
            dns_support=excluded.dns_support, status=excluded.status,
            tags=excluded.tags""",
        (vpc.id, vpc.name, vpc.cidr_block, int(vpc.dns_support),
         vpc.status.value, vpc.created_at, json.dumps(vpc.tags)))
    db.get_db().commit()


def delete_vpc(vpc_id: str) -> bool:
    c = db.get_db().execute(
        "UPDATE vpcs SET status='deleted' WHERE id=? AND status != 'deleted'", (vpc_id,))
    db.get_db().commit()
    return c.rowcount > 0


# --- Subnet ---

def list_subnets() -> list[Subnet]:
    rows = db.get_db().execute("SELECT * FROM subnets WHERE status != 'deleted'").fetchall()
    return [_subnet_from_row(r) for r in rows]


def list_subnets_by_vpc(vpc_id: str) -> list[Subnet]:
    rows = db.get_db().execute(
        "SELECT * FROM subnets WHERE vpc_id=? AND status != 'deleted'", (vpc_id,)).fetchall()
    return [_subnet_from_row(r) for r in rows]


def get_subnet(subnet_id: str) -> Optional[Subnet]:
    row = db.get_db().execute(
        "SELECT * FROM subnets WHERE id=? AND status != 'deleted'", (subnet_id,)).fetchone()
    return _subnet_from_row(row) if row else None


def find_subnet_by_name(name: str) -> Optional[Subnet]:
    row = db.get_db().execute(
        "SELECT * FROM subnets WHERE name=? AND status != 'deleted'", (name,)).fetchone()
    return _subnet_from_row(row) if row else None


def put_subnet(subnet: Subnet) -> None:
    db.get_db().execute("""INSERT INTO subnets
        (id,name,vpc_id,cidr_block,public,zone,status,created_at,tags)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, vpc_id=excluded.vpc_id, cidr_block=excluded.cidr_block,
            public=excluded.public, zone=excluded.zone, status=excluded.status,
            tags=excluded.tags""",
        (subnet.id, subnet.name, subnet.vpc_id, subnet.cidr_block,
         int(subnet.public), subnet.zone, subnet.status.value,
         subnet.created_at, json.dumps(subnet.tags)))
    db.get_db().commit()


def delete_subnet(subnet_id: str) -> bool:
    c = db.get_db().execute(
        "UPDATE subnets SET status='deleted' WHERE id=? AND status != 'deleted'", (subnet_id,))
    db.get_db().commit()
    return c.rowcount > 0


# --- Internet Gateway ---

def list_igws() -> list[InternetGateway]:
    rows = db.get_db().execute(
        "SELECT * FROM internet_gateways WHERE status != 'deleted'").fetchall()
    return [_igw_from_row(r) for r in rows]


def list_igws_by_vpc(vpc_id: str) -> list[InternetGateway]:
    rows = db.get_db().execute(
        "SELECT * FROM internet_gateways WHERE vpc_id=? AND status != 'deleted'",
        (vpc_id,)).fetchall()
    return [_igw_from_row(r) for r in rows]


def get_igw(igw_id: str) -> Optional[InternetGateway]:
    row = db.get_db().execute(
        "SELECT * FROM internet_gateways WHERE id=? AND status != 'deleted'",
        (igw_id,)).fetchone()
    return _igw_from_row(row) if row else None


def find_igw_by_name(name: str) -> Optional[InternetGateway]:
    row = db.get_db().execute(
        "SELECT * FROM internet_gateways WHERE name=? AND status != 'deleted'",
        (name,)).fetchone()
    return _igw_from_row(row) if row else None


def put_igw(igw: InternetGateway) -> None:
    db.get_db().execute("""INSERT INTO internet_gateways
        (id,name,vpc_id,status,created_at,tags)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, vpc_id=excluded.vpc_id,
            status=excluded.status, tags=excluded.tags""",
        (igw.id, igw.name, igw.vpc_id, igw.status.value, igw.created_at,
         json.dumps(igw.tags)))
    db.get_db().commit()


def delete_igw(igw_id: str) -> bool:
    c = db.get_db().execute(
        "UPDATE internet_gateways SET status='deleted' WHERE id=? AND status != 'deleted'",
        (igw_id,))
    db.get_db().commit()
    return c.rowcount > 0


# --- Route Table ---

def list_route_tables() -> list[RouteTable]:
    rows = db.get_db().execute(
        "SELECT * FROM route_tables WHERE status != 'deleted'").fetchall()
    return [_rt_from_row(r) for r in rows]


def list_route_tables_by_vpc(vpc_id: str) -> list[RouteTable]:
    rows = db.get_db().execute(
        "SELECT * FROM route_tables WHERE vpc_id=? AND status != 'deleted'",
        (vpc_id,)).fetchall()
    return [_rt_from_row(r) for r in rows]


def get_route_table(rt_id: str) -> Optional[RouteTable]:
    row = db.get_db().execute(
        "SELECT * FROM route_tables WHERE id=? AND status != 'deleted'", (rt_id,)).fetchone()
    return _rt_from_row(row) if row else None


def find_route_table_by_name(name: str) -> Optional[RouteTable]:
    row = db.get_db().execute(
        "SELECT * FROM route_tables WHERE name=? AND status != 'deleted'",
        (name,)).fetchone()
    return _rt_from_row(row) if row else None


def put_route_table(rt: RouteTable) -> None:
    db.get_db().execute("""INSERT INTO route_tables
        (id,name,vpc_id,subnet_ids,routes,status,created_at,tags)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, vpc_id=excluded.vpc_id, subnet_ids=excluded.subnet_ids,
            routes=excluded.routes, status=excluded.status, tags=excluded.tags""",
        (rt.id, rt.name, rt.vpc_id, json.dumps(rt.subnet_ids), json.dumps(rt.routes),
         rt.status.value, rt.created_at, json.dumps(rt.tags)))
    db.get_db().commit()


def delete_route_table(rt_id: str) -> bool:
    c = db.get_db().execute(
        "UPDATE route_tables SET status='deleted' WHERE id=? AND status != 'deleted'",
        (rt_id,))
    db.get_db().commit()
    return c.rowcount > 0


# --- Instance ---

def list_instances() -> list[Instance]:
    rows = db.get_db().execute(
        "SELECT * FROM instances WHERE status != 'deleted'").fetchall()
    return [_inst_from_row(r) for r in rows]


def get_instance(instance_id: str) -> Optional[Instance]:
    row = db.get_db().execute(
        "SELECT * FROM instances WHERE id=? AND status != 'deleted'", (instance_id,)).fetchone()
    return _inst_from_row(row) if row else None


def find_instance_by_name(name: str) -> Optional[Instance]:
    row = db.get_db().execute(
        "SELECT * FROM instances WHERE name=? AND status != 'deleted'", (name,)).fetchone()
    return _inst_from_row(row) if row else None


def put_instance(instance: Instance) -> None:
    db.get_db().execute("""INSERT INTO instances
        (id,name,image_id,flavor,vpc_id,subnet_id,security_group_ids,user_data,
         private_ip,public_ip,status,created_at,tags,domain_name,
         ssh_host_port,http_host_port,ssh_user,users)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, image_id=excluded.image_id, flavor=excluded.flavor,
            vpc_id=excluded.vpc_id, subnet_id=excluded.subnet_id,
            security_group_ids=excluded.security_group_ids, user_data=excluded.user_data,
            private_ip=excluded.private_ip, public_ip=excluded.public_ip,
            status=excluded.status, tags=excluded.tags, domain_name=excluded.domain_name,
            ssh_host_port=excluded.ssh_host_port, http_host_port=excluded.http_host_port,
            ssh_user=excluded.ssh_user, users=excluded.users""",
        (instance.id, instance.name, instance.image_id, instance.flavor,
         instance.vpc_id, instance.subnet_id,
         json.dumps(instance.security_group_ids), instance.user_data,
         instance.private_ip, instance.public_ip, instance.status.value,
         instance.created_at, json.dumps(instance.tags), instance.domain_name,
         instance.ssh_host_port, instance.http_host_port,
         instance.ssh_user, json.dumps(instance.users)))
    db.get_db().commit()


def list_instances_by_vpc(vpc_id: str) -> list[Instance]:
    rows = db.get_db().execute(
        "SELECT * FROM instances WHERE vpc_id=? AND status NOT IN ('deleted','error')",
        (vpc_id,)).fetchall()
    return [_inst_from_row(r) for r in rows]


def delete_instance_record(instance_id: str) -> bool:
    c = db.get_db().execute(
        "UPDATE instances SET status='deleted' WHERE id=? AND status != 'deleted'", (instance_id,))
    db.get_db().commit()
    return c.rowcount > 0


# --- Load Balancer ---

def list_lbs() -> list[LoadBalancer]:
    rows = db.get_db().execute(
        "SELECT * FROM load_balancers WHERE status != 'deleted'").fetchall()
    return [_lb_from_row(r) for r in rows]


def get_lb(lb_id: str) -> Optional[LoadBalancer]:
    row = db.get_db().execute(
        "SELECT * FROM load_balancers WHERE id=? AND status != 'deleted'", (lb_id,)).fetchone()
    return _lb_from_row(row) if row else None


def find_lb_by_name(name: str) -> Optional[LoadBalancer]:
    row = db.get_db().execute(
        "SELECT * FROM load_balancers WHERE name=? AND status != 'deleted'", (name,)).fetchone()
    return _lb_from_row(row) if row else None


def put_lb(lb: LoadBalancer) -> None:
    db.get_db().execute("""INSERT INTO load_balancers
        (id,name,type,vpc_id,subnet_ids,internal,dns_name,listen_port,backends,listeners,health_check,status,created_at,tags)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, type=excluded.type, vpc_id=excluded.vpc_id,
            subnet_ids=excluded.subnet_ids, internal=excluded.internal,
            dns_name=excluded.dns_name, listen_port=excluded.listen_port,
            backends=excluded.backends, listeners=excluded.listeners,
            health_check=excluded.health_check, status=excluded.status, tags=excluded.tags""",
        (lb.id, lb.name, lb.type, lb.vpc_id, json.dumps(lb.subnet_ids),
         int(lb.internal), lb.dns_name, lb.listen_port, json.dumps(lb.backends),
         json.dumps(lb.listeners), json.dumps(lb.health_check),
         lb.status.value, lb.created_at, json.dumps(lb.tags)))
    db.get_db().commit()


def delete_lb(lb_id: str) -> bool:
    c = db.get_db().execute(
        "UPDATE load_balancers SET status='deleted' WHERE id=? AND status != 'deleted'", (lb_id,))
    db.get_db().commit()
    return c.rowcount > 0
