#!/usr/bin/env python

# Copyright 2016 Amazon.com, Inc. or its affiliates.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#    http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

import json
import re
from jinja2 import Template as jinja_template
from troposphere import Output, GetAtt, Ref, Export, Sub, ImportValue
from troposphere.iam import Role, ManagedPolicy, Group, User
from troposphere.iam import InstanceProfile, LoginProfile
from lib.config_helper import config


# CloudFormation names must be alphanumeric.
# Our config might include non-alpha, so we'll scrub them here.
def scrub_name(name):
    return(re.sub('[\W_]+', '', name))


# Creates a policy document from a jinja template
def policy_document_from_jinja(c, policy_name, model):

    # Try and read the policy file file into a jinja template object
    try:
        template = jinja_template(
            open("../policy/" + model["policy_file"]).read()
        )
    except Exception as e:
        raise ValueError(
            "Failed to read template file ../policy/{}\n\n{}".format(
                model["policy_file"],
                e
            )
        )

    # Perform our jinja substitutions on the file contents.
    template_vars = ""
    if "template_vars" in model:
        template_vars = model["template_vars"]

    try:
        template_jinja = template.render(
            config=c.config,
            account=c.map_account(c.current_account),
            parent_account=c.parent_account_id,
            template_vars=template_vars
        )
    except Exception as e:
        raise ValueError(
            "Jinja render failure working on file ../policy/{}\n\n{}".format(
                model["policy_file"],
                e
            )
        )

    # Now encode the jinja parsed template as JSON
    try:
        template_json = json.loads(template_jinja)
    except Exception as e:
        print(
            "Contents returned after Jinja parsing:\n{}".format(
                template_jinja
            )
        )
        raise ValueError(
            "JSON encoding failure working on file ../policy/{}\n\n{}".format(
                model["policy_file"],
                e
            )
        )

    return(template_json)


def build_role_trust(c, trusts):
    policy = {
        "Version":  "2012-10-17",
        "Statement": [],
    }

    sts_principals = []
    saml_principals = []
    for trust in trusts:
        # See if we match an account:
        # First see if we match an account friendly name.
        trust_account = c.search_accounts([trust])
        if trust_account:
            sts_principals.append({
                "AWS": "arn:aws:iam::" +
                       str(c.account_map_ids[trust_account[0]]) +
                       ":root"
            })
        # Next see if we match our SAML trust.
        elif trust == c.saml_provider:
            saml_principals.append({
                "Federated": "arn:aws:iam::" +
                             c.parent_account_id +
                             ":saml-provider/" +
                             c.saml_provider
            })
        # See if we have a 'dot' in our name denoting a service.
        elif re.match("^.*\..*?$", trust):
            sts_principals.append({"Service": trust})
        # otherwise this is likely an account friendly name that isn't correct.
        else:
            raise ValueError(
                "Uanble to find trust name '{}' in the config.yaml. "
                "Assure it exists in the account section.".format(
                    trust
                )
            )

    for sts_principal in sts_principals:
        policy["Statement"].append({
            "Effect": "Allow",
            "Principal": sts_principal,
            "Action": "sts:AssumeRole"
        })

    for saml_principal in saml_principals:
        policy["Statement"].append({
            "Effect": "Allow",
            "Principal": saml_principal,
            "Action": "sts:AssumeRoleWithSAML",
            "Condition": {
                "StringEquals": {
                    "SAML:aud": "https://signin.aws.amazon.com/saml"
                }
            }
        })

    return(policy)


def build_assume_role_policy_document(c, accounts, roles):
    policy_statement = {
        "Version": "2012-10-17",
        "Statement": []
    }
    for role in roles:
        for account in accounts:
            policy_statement["Statement"].append(
                build_sts_statement(c.map_account(account), role)
            )

    return(policy_statement)


def build_sts_statement(account, role):
    statement = {
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Resource": "arn:aws:iam::" + account + ":role/" + role,
    }
    return(statement)


# Managed policies are unique in that they must be an ARN.
# So either we have an ARN, or a Ref() within our current environment
# or an import: statement from another cloudformation template.
def parse_managed_policies(c, managed_policies, working_on):
    managed_policy_list = []
    for managed_policy in managed_policies:
        # If we have an ARN then we're explicit
        if re.match("arn:aws", managed_policy):
            managed_policy_list.append(managed_policy)
        # If we have an import: then we're importing from another template.
        elif re.match("^import:", managed_policy):
            m = re.match("^import:(.*)", managed_policy)
            managed_policy_list.append(ImportValue(m.group(1)))
        # Alternately we're dealing with a managed policy locally that
        # we need to 'Ref' to get an ARN.
        else:
            # Confirm this is a local policy, otherwise we'll error out.
            if c.is_local_managed_policy(managed_policy):
                # Policy name exists in the template,
                # lets make sure it will exist in this account.
                if c.is_managed_policy_in_account(
                        managed_policy,
                        c.map_account(c.current_account)
                ):
                    # If this is a ref we'll need to assure it's scrubbed
                    managed_policy_list.append(Ref(scrub_name(managed_policy)))
                else:
                    raise ValueError(
                        "Working on: '{}' - Managed Policy: '{}' "
                        "is not configured to go into account: '{}'".format(
                            working_on,
                            managed_policy,
                            c.current_account
                        )
                    )
            else:
                raise ValueError(
                    "Working on: '{}' - Managed Policy: '{}' "
                    "does not exist in the configuration file".format(
                        working_on,
                        managed_policy
                    )
                )

    return(managed_policy_list)


# Users, Groups and roles are simply by name versus an ARN.
# We will take them at face value since there's no way to verify their syntax.
# We will however check for import: values and substitute accordingly.
def parse_imports(c, element_list):
    return_list = []
    for element in element_list:
        # See if we match an import
        if re.match("^import:", element):
            m = re.match("^import:(.*)", element)
            return_list.append(ImportValue(m.group(1)))
        # Otherwise we're verbatim as there's no real way to know if this
        # is within the template or existing.
        else:
            return_list.append(element)

    return(return_list)


def add_managed_policy(
        c,
        ManagedPolicyName,
        PolicyDocument,
        model,
        named=False
        ):
    cfn_name = scrub_name(ManagedPolicyName)
    kw_args = {
        "Description": "Managed Policy " + ManagedPolicyName,
        "PolicyDocument": PolicyDocument,
        "Groups": [],
        "Roles": [],
        "Users": []
    }

    if named:
        kw_args["ManagedPolicyName"] = ManagedPolicyName
    if "description" in model:
        kw_args["Description"] = model["description"]
    if "groups" in model:
        kw_args["Groups"] = parse_imports(c, model["groups"])
    if "users" in model:
        kw_args["Users"] = parse_imports(c, model["users"])
    if "roles" in model:
        kw_args["Roles"] = parse_imports(c, model["roles"])

    if "retain_on_delete" in model:
        if model["retain_on_delete"] is True:
            kw_args["DeletionPolicy"] = "Retain"

    c.template[c.current_account].add_resource(ManagedPolicy(
        cfn_name,
        **kw_args
    ))

    if c.config['global']['template_outputs'] == "enabled":
        c.template[c.current_account].add_output([
            Output(
                cfn_name + "PolicyArn",
                Description=kw_args["Description"] + " Policy Document ARN",
                Value=Ref(cfn_name),
                Export=Export(Sub(
                        "${AWS::StackName}-"
                        + cfn_name
                        + "PolicyArn"
                        ))
            )
        ])


def create_instance_profile(c, RoleName, model, named=False):
    cfn_name = scrub_name(RoleName + "InstanceProfile")

    kw_args = {
        "Path": "/",
        "Roles": [Ref(scrub_name(RoleName + "Role"))]
    }

    if named:
        kw_args["InstanceProfileName"] = RoleName

    if "retain_on_delete" in model:
        if model["retain_on_delete"] is True:
            kw_args["DeletionPolicy"] = "Retain"

    c.template[c.current_account].add_resource(InstanceProfile(
        cfn_name,
        **kw_args
    ))

    if c.config['global']['template_outputs'] == "enabled":
        c.template[c.current_account].add_output([
            Output(
                cfn_name + "Arn",
                Description="Instance profile for Role " + RoleName + " ARN",
                Value=Ref(cfn_name),
                Export=Export(Sub("${AWS::StackName}-" + cfn_name + "Arn"))
            )
        ])


def add_role(c, RoleName, model, named=False):
    cfn_name = scrub_name(RoleName + "Role")
    kw_args = {
        "Path": "/",
        "AssumeRolePolicyDocument": build_role_trust(c, model['trusts']),
        "ManagedPolicyArns": [],
        "Policies": []
    }

    if named:
        kw_args["RoleName"] = RoleName

    if "managed_policies" in model:
        kw_args["ManagedPolicyArns"] = parse_managed_policies(
                                        c, model["managed_policies"], RoleName)

    if "retain_on_delete" in model:
        if model["retain_on_delete"] is True:
            kw_args["DeletionPolicy"] = "Retain"

    c.template[c.current_account].add_resource(Role(
        cfn_name,
        **kw_args
    ))
    if c.config['global']['template_outputs'] == "enabled":
        c.template[c.current_account].add_output([
            Output(
                cfn_name + "Arn",
                Description="Role " + RoleName + " ARN",
                Value=GetAtt(cfn_name, "Arn"),
                Export=Export(Sub("${AWS::StackName}-" + cfn_name + "Arn"))
            )
        ])


def add_group(c, GroupName, model, named=False):
    cfn_name = scrub_name(GroupName + "Group")
    kw_args = {
        "Path": "/",
        "ManagedPolicyArns": [],
        "Policies": []
    }

    if named:
        kw_args["GroupName"] = GroupName

    if "managed_policies" in model:
        kw_args["ManagedPolicyArns"] = parse_managed_policies(
            c,
            model["managed_policies"], GroupName
        )

    if "retain_on_delete" in model:
        if model["retain_on_delete"] is True:
            kw_args["DeletionPolicy"] = "Retain"

    c.template[c.current_account].add_resource(Group(
        scrub_name(cfn_name),
        **kw_args
    ))
    if c.config['global']['template_outputs'] == "enabled":
        c.template[c.current_account].add_output([
            Output(
                cfn_name + "Arn",
                Description="Group " + GroupName + " ARN",
                Value=GetAtt(cfn_name, "Arn"),
                Export=Export(Sub("${AWS::StackName}-" + cfn_name + "Arn"))
            )
        ])


def add_user(c, UserName, model, named=False):
    cfn_name = scrub_name(UserName + "User")
    kw_args = {
        "Path": "/",
        "Groups": [],
        "ManagedPolicyArns": [],
        "Policies": [],
    }

    if named:
        kw_args["UserName"] = UserName

    if "groups" in model:
        kw_args["Groups"] = parse_imports(c, model["groups"])

    if "managed_policies" in model:
        kw_args["ManagedPolicyArns"] = parse_managed_policies(
                c,
                model["managed_policies"],
                UserName
            )

    if "password" in model:
        kw_args["LoginProfile"] = LoginProfile(
            Password=model["password"],
            PasswordResetRequired=True
        )

    if "retain_on_delete" in model:
        if model["retain_on_delete"] is True:
            kw_args["DeletionPolicy"] = "Retain"

    c.template[c.current_account].add_resource(User(
        cfn_name,
        **kw_args
    ))
    if c.config['global']['template_outputs'] == "enabled":
        c.template[c.current_account].add_output([
            Output(
                cfn_name + "Arn",
                Description="User " + UserName + " ARN",
                Value=GetAtt(cfn_name, "Arn"),
                Export=Export(Sub("${AWS::StackName}-" + cfn_name + "Arn"))
            )
        ])


try:
    c = config("../config.yaml")
except Exception as e:
    raise ValueError(
        "Failed to parse the YAML Configuration file. "
        "Check your syntax and spacing!\n\n{}".format(e)
    )

# We introduced a 'global' section to control naming now that we have more
# control over naming via Cloudformation.  To be backward compatible with
# older config.yamls that don't have a config.yaml we'll set our values
# to our previous implied functionality (named values for all but managed
# policies).
if 'global' not in c.config:
    c.config['global'] = {
        "names": {
            "policies": False,
            "roles": True,
            "users": True,
            "groups": True
        },
        "template_outputs": "enabled"
    }

# Policies
if "policies" in c.config:
    for policy_name in c.config["policies"]:

        context = ["all"]
        if "in_accounts" in c.config["policies"][policy_name]:
            context = c.config["policies"][policy_name]["in_accounts"]

        for account in c.search_accounts(context):
            c.current_account = account
            # If our managed policy is jinja based we'll have a policy_file
            policy_document = ""
            if "policy_file" in c.config["policies"][policy_name]:
                policy_document = policy_document_from_jinja(
                    c,
                    policy_name,
                    c.config["policies"][policy_name]
                )
            # If our managed policy is generated as an assume trust
            # we'll have assume
            if "assume" in c.config["policies"][policy_name]:
                policy_document = build_assume_role_policy_document(
                    c,
                    c.search_accounts(
                        c.config["policies"][policy_name]["assume"]["accounts"]
                    ),
                    c.config["policies"][policy_name]["assume"]["roles"]
                )

            add_managed_policy(
                c,
                policy_name,
                policy_document,
                c.config["policies"][policy_name],
                c.config["global"]["names"]["policies"]
            )

# Roles
if "roles" in c.config:
    for role_name in c.config["roles"]:
        context = ["all"]
        if "in_accounts" in c.config["roles"][role_name]:
            context = c.config["roles"][role_name]["in_accounts"]

        for account in c.search_accounts(context):
            c.current_account = account
            add_role(
                c,
                role_name,
                c.config["roles"][role_name],
                c.config["global"]["names"]["roles"]
            )

            # See if we need to add an instance profile too with an ec2 trust.
            if "ec2.amazonaws.com" in c.config["roles"][role_name]["trusts"]:
                create_instance_profile(
                    c,
                    role_name,
                    c.config["roles"][role_name],
                    c.config["global"]["names"]["roles"]
                )

# Groups
if "groups" in c.config:
    for group_name in c.config["groups"]:

        context = ["all"]
        if "in_accounts" in c.config["groups"][group_name]:
            context = c.config["groups"][group_name]["in_accounts"]

        for account in c.search_accounts(context):
            c.current_account = account
            add_group(
                c,
                group_name,
                c.config["groups"][group_name],
                c.config["global"]["names"]["groups"]
            )

# Users
if "users" in c.config:
    for user_name in c.config["users"]:

        context = ["all"]
        if "in_accounts" in c.config["users"][user_name]:
            context = c.config["users"][user_name]["in_accounts"]

        for account in c.search_accounts(context):
            c.current_account = account
            add_user(
                c,
                user_name,
                c.config["users"][user_name],
                c.config["global"]["names"]["users"]
            )

for account in c.search_accounts(["all"]):
    fh = open(
        "../output_templates/"
        + account
        + "(" + c.account_map_ids[account]
        + ")-IAM.template", 'w'
    )
    fh.write(c.template[account].to_json())
    fh.close()
