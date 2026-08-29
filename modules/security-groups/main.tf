# Security Groups module
#
# CloudCore does not have a security group API resource. This module
# manages security group definitions as structured configuration and
# produces stable ID strings consumed by the compute module's
# security_group_ids field.
#
# When CloudCore adds a security group API resource, this module's
# main.tf will be the only file that needs updating — all callers
# continue to consume outputs unchanged.
