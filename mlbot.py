import click
import boto3
import sys
import yaml
import subprocess
import tempfile
import requests
import os
import json
import time
from collections import defaultdict
from yaspin import yaspin

CONFIG_FOLDER_NAME = ".mlbot"

# TODO: Figure out a better way to install these k8s services
K8S_DASHBOARD_MANIFEST_URL = "https://raw.githubusercontent.com/kubernetes/dashboard/v2.5.0/aio/deploy/recommended.yaml"
K8S_CLUSTER_AUTOSCALER_EXAMPLE_TEMPLATE_URL = "https://raw.githubusercontent.com/kubernetes/autoscaler/master/cluster-autoscaler/cloudprovider/aws/examples/cluster-autoscaler-autodiscover.yaml"
K8S_METRICS_SERVER_MANIFEST_URL = "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"

INSTANCE_TYPE_X_RESOURCES = {
    "p3dn.24xlarge" : {
        "gpus" : 8,
        "hugepages-2Mi" : "5120Mi",
        "efa" : 1,
        "memory" : "600G"
    },
    "p4d.24xlarge" : {
        "gpus" : 8,
        "hugepages-2Mi" : "5120Mi",
        "efa" : 4,
        "memory" : "900G"
    }
}

def create_nodegroup_name(spot, instance_type, availability_zone):
    prefix = "spot" if spot else "od"
    return f"{prefix}-{instance_type.replace('.','')}-{availability_zone}"

def create_gpu_nodegroup(cluster_name, instance_type, availability_zone, min_size, max_size, is_spot):
    name = create_nodegroup_name(spot=is_spot, instance_type=instance_type, availability_zone=availability_zone)
    
    efa_enabled = instance_type in INSTANCE_TYPE_X_RESOURCES
    
    ng = {
        "name" : name,
        "availabilityZones" : [availability_zone,],
        "minSize": min_size,
        "maxSize" : max_size,
        "volumeSize" : 100,
        "labels" : {
            "nvidia.com/gpu": "true",
            "gpu_ng" : name,
        },
        "taints" : [
            {"key" : "nvidia.com/gpu", "value" : "true", "effect": "NoSchedule"}
        ],
        "tags" : {
            "k8s.io/cluster-autoscaler/node-template/taint/nvidia.com/gpu": "true:NoSchedule",
            "k8s.io/cluster-autoscaler/node-template/label/nvidia.com/gpu": "true",
            "k8s.io/cluster-autoscaler/node-template/label/gpu_ng": name,
            "k8s.io/cluster-autoscaler/enabled": 'true',
            f"k8s.io/cluster-autoscaler/{cluster_name}": 'true'
        },
        "efaEnabled": efa_enabled,
        "privateNetworking": True,
        "iam" : {
            "withAddonPolicies" : {
                "autoScaler": True,
            }
        }
    }
    
    if is_spot:
        ng['instancesDistribution'] = {
            'instanceTypes': [instance_type],
            'onDemandBaseCapacity': 0,
            'onDemandPercentageAboveBaseCapacity': 0,
            'spotAllocationStrategy': "capacity-optimized"
        }
    else:
        ng['instanceType'] = instance_type
    return ng


def get_az_instance_type_availabilities(instance_types, region):
    ec2 = boto3.client('ec2', region_name=region)
    response = ec2.describe_instance_type_offerings(LocationType='availability-zone', Filters=[{'Name': 'instance-type', 'Values': instance_types}])['InstanceTypeOfferings']
    
    az_x_instance_types = defaultdict(set)
    for item in response:
        az_x_instance_types[item['Location']].add(item['InstanceType'])
    
    return az_x_instance_types

def is_valid_config(config, validate_project_config=True):
    if validate_project_config:
        if "project" not in config:
            return False
    
        for k in {"name", "docker_image"}:
            if not config["project"].get(k):
                return False
    
    for k in {"name", "region", "version", "availabilityZones", "nodegroups"}:
        if not config["cluster"].get(k):
            return False
    
    for k in {"standard", 'gpu'}:
        if not config['cluster']["nodegroups"].get(k):
            return False
    return True

def run_progress_step(description, fn, color="cyan"):
    with yaspin(text=description, color=color) as spinner:
        success, err = fn()
        if success:
            spinner.ok("✅ ")
        else:
            spinner.fail("❗️ ")
            print("\nError:")
            print(err)
            sys.exit(1)


@click.group()
def cli():
    pass

@cli.command()
@click.option('--project', type=str, help="The name for this project. This will also be the prefix for all associated compute jobs", required=True)
@click.option('--docker-image', type=str, help="The full docker image name to use for building & pushing this project's code", required=True)
def init(project, docker_image):
    config_dir = os.path.join(os.getcwd(), CONFIG_FOLDER_NAME)
    config_filepath = os.path.join(config_dir, "config.json")
    try:
        config = json.load(open(config_filepath))
    except:
        print("MLbot isn't properly configured yet. To do this, first run 'mlbot create-cluster'.")
        sys.exit(1)
    
    os.makedirs(config_dir, exist_ok=True)
    config['project'] = {
        "name" : project,
        "docker_image" : docker_image
    }

    with open(config_filepath, 'w') as f:
        f.write(json.dumps(config, indent=4))

@cli.command()
@click.option('--cluster', type=str, help="EKS cluster's name", required=True)
@click.option('--version', type=str, help="EKS cluster's k8s version", required=True)
@click.option('--region', type=str, help="EKS cluster's AWS region", required=True)
@click.option('--availability-zones', type=str, help="EKS cluster's AWS availability zones (separated by commas)", required=True)
@click.option('--instance-types', type=str, help="GPU instance types to use (separated by commas)", required=True)
@click.option('--min-gpu-nodes', type=int, help="Minimum number of nodes for each GPU node group", default=0)
@click.option('--max-gpu-nodes', type=int, help="Maximum number of nodes for each GPU node group", required=True)
@click.option('--standard-instance-type', type=str, help="Instance type to use for the standard (non-GPU) node group", default="m5.xlarge")
@click.option('--min-standard-nodes', type=int, help="Minimum number of nodes for the standard (non-GPU) node group", default=0)
@click.option('--max-standard-nodes', type=int, help="Maximum number of nodes for the standard (non-GPU) node group", default=5)
@click.option('--create/--no-create', help="If true, this will automatically create an EKS cluster using eksctl", default=False)
def create_cluster(cluster, version, region, availability_zones, instance_types, min_gpu_nodes, max_gpu_nodes, standard_instance_type="m5.xlarge", min_standard_nodes=0, max_standard_nodes=5, create=False):
    config_dir = os.path.join(os.getcwd(), CONFIG_FOLDER_NAME)
    if os.path.exists(config_dir):
        print(f"MLbot seems to already be configured in the current directory. To run this command, please delete the '{CONFIG_FOLDER_NAME}' folder or change to a new folder and try again.")
        sys.exit(1)

    availability_zones = [az.strip().lower() for az in availability_zones.split(",")]
    instance_types = [it.strip().lower() for it in instance_types.split(",")]
    
    for instance_type in instance_types:
        if instance_type not in INSTANCE_TYPE_X_RESOURCES:
            print(f"Unsupported instance type: {instance_type}")
            sys.exit(1)
    
    cluster_config = {
        "apiVersion" : "eksctl.io/v1alpha5",
        "kind" : "ClusterConfig",
        "metadata": {
            "name" : cluster,
            "region" : region,
            "version" : version
        },
        "availabilityZones": availability_zones,
        "nodeGroups" : []
    }
    config = {
        "cluster" : {
            "name" : cluster,
            "region" : region,
            "version" : version,
            "availabilityZones": availability_zones,
            "nodegroups" : {
                "standard" : [],
                "gpu" : []
            }
        }
    }
    
    az_x_instance_types = get_az_instance_type_availabilities(instance_types=instance_types, region=region)
    if not az_x_instance_types:
        print("Provided instance types are not available in the provided availability zones.")
        sys.exit(1)
        
    # Add the standard nodegroup (with non-GPU nodes)
    config['cluster']['nodegroups']['standard'].append({"name" : "standard-ng-1", "instanceType" : standard_instance_type})
    cluster_config['nodeGroups'].append({
        "name" : "standard-ng-1",
        "instanceType" : standard_instance_type,
        "minSize" : min_standard_nodes,
        "maxSize" : max_standard_nodes,
        "volumeSize" : 100,
        "tags" : {
            "k8s.io/cluster-autoscaler/enabled": 'true',
            f"k8s.io/cluster-autoscaler/{cluster}": 'true'
        },
        "iam" : {
            "withAddonPolicies" : {
                "autoScaler": True,
            }
        }
    })
    
    # Add the gpu node groups (spot & on-demand)
    for az, instance_types in az_x_instance_types.items():
        for instance_type in instance_types:
            # add spot & on-demand version of this node group
            gpu_nodegroup = create_gpu_nodegroup(
                    cluster_name=cluster, 
                    instance_type=instance_type, 
                    availability_zone=az, 
                    min_size=min_gpu_nodes, 
                    max_size=max_gpu_nodes, 
                    is_spot=True
                )
            config['cluster']['nodegroups']['gpu'].append({"name" : gpu_nodegroup['name'], "availability_zone" : az, "instance_type" : instance_type, "spot" : True})
            cluster_config['nodeGroups'].append(gpu_nodegroup)
            
            gpu_nodegroup = create_gpu_nodegroup(
                    cluster_name=cluster, 
                    instance_type=instance_type, 
                    availability_zone=az, 
                    min_size=min_gpu_nodes, 
                    max_size=max_gpu_nodes, 
                    is_spot=False
                )
            config['cluster']['nodegroups']['gpu'].append({"name" : gpu_nodegroup['name'], "availability_zone" : az, "instance_type" : instance_type, "spot" : False})
            cluster_config['nodeGroups'].append(gpu_nodegroup)
    
    os.makedirs(config_dir, exist_ok=False)
    config_filepath = os.path.join(config_dir, "config.json")
    with open(config_filepath, 'w') as f:
        f.write(json.dumps(config, indent=4))

    # Save EKS cluster config
    filename = os.path.join(config_dir, "cluster.yaml")
    with open(filename, "w") as file:
        yaml.dump(cluster_config, file)
    print(f"Saved EKS cluster config to: {filename}\n")

    if not create:
        print(f"To create the EKS cluster, run: eksctl create cluster -f {filename}")
    else:
        # Use eksctl to create the EKS cluster
        def create_eks_cluster_with_eksctl():
            subprocess.check_call(['eksctl', 'create', 'cluster', '-f', filename], stdout=sys.stdout, stderr=subprocess.STDOUT)
        
        print("Creating EKS cluster with eksctl (this can take several minutes)...\n\n")
        create_eks_cluster_with_eksctl()

@cli.command()
def setup_cluster():
    print ("Setting up the cluster...\n")
    iam_client = boto3.client('iam')
    
    config_dir = os.path.join(os.getcwd(), CONFIG_FOLDER_NAME)
    config_filepath = os.path.join(config_dir, "config.json")
    
    try:
        config = json.load(open(config_filepath))
        assert is_valid_config(config, validate_project_config=False)
    except:
        print("MLbot isn't properly configured yet. To do this, first run 'mlbot create-cluster'.")
        sys.exit(1)
    
    # 1. Install the k8s dashboard
    def install_k8s_dashboard():
        process = subprocess.run(['kubectl', 'apply', '-f', K8S_DASHBOARD_MANIFEST_URL], 
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        return success, err
    
    run_progress_step("Installing the k8s dashboard", install_k8s_dashboard)
    
    # 2. Create an IAM OIDC provider for the cluster
    def create_iam_oidc_provider():
        process = subprocess.run(['eksctl', 'utils', 'associate-iam-oidc-provider', '--cluster', config['cluster']['name'], '--approve'], 
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        return success, err
    
    run_progress_step("Creating an IAM OIDC provider", create_iam_oidc_provider)
        
    # 3. Setup the cluster auto-scaler
    cluster_policy_config = {"name" : f"MLBot-EKSClusterAutoscaler-{int(time.time())}"}
    
    def create_cluster_autoscaler_policy_arn(cluster_policy_config):
        iam_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": [
                        "autoscaling:DescribeAutoScalingGroups",
                        "autoscaling:DescribeAutoScalingInstances",
                        "autoscaling:DescribeLaunchConfigurations",
                        "autoscaling:DescribeTags",
                        "autoscaling:SetDesiredCapacity",
                        "autoscaling:TerminateInstanceInAutoScalingGroup",
                        "ec2:DescribeLaunchTemplateVersions"
                    ],
                    "Resource": "*",
                    "Effect": "Allow"
                }
            ]
        }
        try:
            response = iam_client.create_policy(
                PolicyName=cluster_policy_config['name'],
                PolicyDocument=json.dumps(iam_policy),
                Description="IAM policy used by MLBot's EKS cluster autoscaler",
            )
            cluster_policy_config['arn'] = response['Policy']['Arn']
            return True, None
        except Exception as e:
            return False, str(e)
        
    run_progress_step("Creating the cluster auto-scaler's IAM policy", lambda : create_cluster_autoscaler_policy_arn(cluster_policy_config))
    config['cluster']['autoscaler_policy_arn'] = cluster_policy_config['arn']
    
    def setup_cluster_autoscaler():
        process = subprocess.run([
                        'eksctl', 
                        'create', 
                        'iamserviceaccount', 
                        '--cluster', config['cluster']['name'], 
                        '--namespace', 'kube-system',
                        '--name', 'cluster-autoscaler',
                        '--attach-policy-arn', cluster_policy_config['arn'],
                        '--override-existing-serviceaccounts',
                        '--approve'
                    ], 
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        if not success:
            return success, err
        
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as f:
            resp = requests.get(K8S_CLUSTER_AUTOSCALER_EXAMPLE_TEMPLATE_URL, allow_redirects=True)
            if resp.status_code != 200:
                return False, f"Downloading the k8s cluster autoscaler template from {K8S_CLUSTER_AUTOSCALER_EXAMPLE_TEMPLATE_URL} failed."
            manifest_text = resp.text
            assert "<YOUR CLUSTER NAME>" in manifest_text
            manifest_text = manifest_text.replace("<YOUR CLUSTER NAME>", config['cluster']['name'])
            f.write(manifest_text)
            f.flush()
            
            process = subprocess.run([
                            'kubectl', 
                            'apply', 
                            '-f', 
                            f.name
                        ], 
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        universal_newlines=True)
        
        success = process.returncode == 0
        err = process.stderr
        
        return success, err
        
    run_progress_step("Setting up the autoscaler", setup_cluster_autoscaler)
    
    # 4. Setup the metrics server
    def setup_metrics_server():
        process = subprocess.run([
                        'kubectl', 
                        'apply', 
                        '-f', 
                        K8S_METRICS_SERVER_MANIFEST_URL
                    ], 
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        return success, err
        
    run_progress_step("Setting up the metrics server", setup_metrics_server)
    
    # 5. Setup torchelastic's k8s operator
    def setup_torchelastic_k8s_operator():
        with tempfile.TemporaryDirectory() as tmpdirname:
            process = subprocess.run([
                        'git', 'clone', 'https://github.com/pytorch/elastic.git'
                    ], 
                    cwd=tmpdirname,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
            success = process.returncode == 0
            err = process.stderr
            
            if not success:
                return success, err
            
            process = subprocess.run([
                        'kubectl', 'apply', '-k', 'config/default',
                    ], 
                    cwd=os.path.join(tmpdirname, "elastic", "kubernetes"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
            success = process.returncode == 0
            err = process.stderr
        return success, err
    
    run_progress_step("Setting up the TorchElastic k8s controller", setup_torchelastic_k8s_operator)
    
    # 6. Setup etcd for torchelastic
    def setup_etcd():
        etcd_manifest_filepath = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), 
            "artifacts", 
            "cluster", 
            "eks", 
            "torchelastic", 
            "etcd.yaml"
        )
        process = subprocess.run([
                        'kubectl', 'apply', '-f', etcd_manifest_filepath
                    ], 
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        return success, err
    
    run_progress_step("Setting up etcd for TorchElastic", setup_etcd)
    
    # 7. Patch the k8s EFA plugin to enable the daemonset to run on GPU nodes
    def patch_efa_plugin():
        patch = {
            "spec" : {
                "template" : {
                    "spec" : {
                        "tolerations" : [
                            {
                                "key" : "nvidia.com/gpu",
                                "operator" : "Exists",
                                "effect" : "NoSchedule"
                            }    
                        ]
                    }
                }
            }
        }
        patch = json.dumps(patch)
        process = subprocess.run([
                        'kubectl', '-n', 'kube-system',
                        'patch', 'daemonset', 'aws-efa-k8s-device-plugin-daemonset', 
                        '--type', 'merge', 
                        '--patch', patch
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        return success, err
    
    run_progress_step("Patching the EFA plugin to run on GPU nodes", patch_efa_plugin)
    
    # Save updated mlbot config with the cluster autoscaler's IAM policy ARN
    with open(config_filepath, 'w') as f:
        f.write(json.dumps(config, indent=4))
    
    print()
    print("Your EKS cluster has been setup! Happy training! :)")

        
def prepare_torchelastic_job_k8s_spec(job_id, image_uri, entrypoint, min_num_nodes, max_num_nodes, nodegroup, resources):
    env = [
        {"name" : "JOB_ID", "value" : job_id},
        
        {"name" : "NCCL_DEBUG", "value" : "INFO"},
        {"name" : "NCCL_SOCKET_IFNAME", "value" : "eth0"},
        {"name" : "NCCL_ASYNC_ERROR_HANDLING", "value" : "1"},
    ]
    
    spec = {
        "apiVersion": "elastic.pytorch.org/v1alpha1",
        "kind": "ElasticJob",
        "metadata" : {
            "name" : job_id,
            "namespace" : "elastic-job"
        },
        "spec": {
            "rdzvEndpoint" : "etcd-service.elastic-job:2379",
            "minReplicas" : min_num_nodes,
            "maxReplicas" : max_num_nodes,
            "replicaSpecs" : {
                "Worker" : {
                    "replicas" : max_num_nodes,
                    "restartPolicy" : "ExitCode",
                    "template" : {
                        "apiVersion" : "v1",
                        "kind" : "Pod",
                        "spec" : {
                            "tolerations" : [
                                {
                                    "key" : "nvidia.com/gpu",
                                    "operator" : "Equal",
                                    "value" : "true",
                                    "effect" : "NoSchedule"
                                }
                            ],
                            "nodeSelector" : {
                                "gpu_ng" : nodegroup
                            },
                            "volumes" : [
                                {
                                    "name" : "dshm",
                                    "emptyDir" : {
                                        "medium" : "Memory"
                                    }
                                }
                            ],
                            "containers" : [
                                {
                                    "name" : "elastic-job-worker",
                                    "image" : image_uri,
                                    "imagePullPolicy" : "Always",
                                    "volumeMounts" : [{
                                        "mountPath" : "/dev/shm",
                                        "name" : "dshm"
                                    }],
                                    "command": ["torchrun"],
                                    "args" : [
                                        f"--nproc_per_node={resources['gpus']}",
                                    ] + entrypoint,
                                    "env" : env,
                                    "resources" : {
                                        "requests" : {
                                            "nvidia.com/gpu": resources['gpus'],
                                            "hugepages-2Mi": resources['hugepages-2Mi'],
                                            "vpc.amazonaws.com/efa": resources['efa'],
                                            "memory": resources['memory'],
                                        },
                                        "limits" : {
                                            "nvidia.com/gpu": resources['gpus'],
                                            "hugepages-2Mi": resources['hugepages-2Mi'],
                                            "vpc.amazonaws.com/efa": resources['efa'],
                                            "memory": resources['memory'],
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    
    return spec
    
    
def get_nodegroup_from_config(config, instance_type, availability_zone=None, spot=False):
    gpu_nodegroups = config['cluster']['nodegroups']['gpu']

    for ng in gpu_nodegroups:
        if ng['instance_type'] == instance_type and ng['spot'] == spot:
            if not availability_zone or (availability_zone and ng['availability_zone'] == availability_zone):
                return ng['name']
    return None
    
@cli.command('run', context_settings=dict(ignore_unknown_options=True))
@click.option('--instance-type', type=str, help="The instance type to use", required=True)
@click.option('--az', type=str, help="The availability zone to use", required=False)
@click.option('--spot/--no-spot', help="Whether to use spot instances", default=False)
@click.option('--num-nodes', type=int, help="The fixed number of nodes for the training job (cannot be used with min/max-nodes)", default=None)
@click.option('--min-nodes', type=int, help="The minimum number of nodes for the elastic training job", default=None)
@click.option('--max-nodes', type=int, help="The maximum number of nodes for the elastic training job", default=None)
@click.option('--dockerfile', type=str, help="Dockerfile to use for building the training job's docker image")
@click.argument('entrypoint', nargs=-1)
def run(instance_type, az, spot, num_nodes, min_nodes, max_nodes, dockerfile, entrypoint):
    config_dir = os.path.join(os.getcwd(), CONFIG_FOLDER_NAME)
    config_filepath = os.path.join(config_dir, "config.json")
    
    try:
        config = json.load(open(config_filepath))
        assert is_valid_config(config)
    except:
        print("MLbot isn't properly configured for this project yet. To do this, first run 'mlbot init' before running this command.")
        sys.exit(1)
    
    if entrypoint:
        entrypoint = list(entrypoint)
    else:
        entrypoint = []
    
    ng = get_nodegroup_from_config(config, instance_type, availability_zone=az, spot=spot)
    if not ng:
        print("Couldn't find a node-group in this cluster with the specified instance type, spot configuration, and availability zone.")
        sys.exit(1)
    
    if num_nodes is not None:
        if min_nodes is not None or max_nodes is not None:
            print("You must specify exactly one of: 'num-nodes' or 'min/max-nodes'.")
            sys.exit(1)
        
        min_nodes = num_nodes
        max_nodes = num_nodes
    else:
        if min_nodes is None or max_nodes is None:
            print("You must specify exactly one of: 'num-nodes' or 'min/max-nodes'.")
            sys.exit(1)
            
    if dockerfile:
        dockerfile_path = os.path.join(os.getcwd(), dockerfile)
        if not os.path.exists(dockerfile_path):
            print("Could not find the specified docker file.")
            sys.exit(1)
    else:
        # user didn't specify a dockerfile to use, so we'll just build the training job using the default dockerfile for mlbot
        dockerfile_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "artifacts", "docker", "Dockerfile")
    
    # Package the code and prepare the deployment
    def prepare_deployment():
        # Build docker image
        process = subprocess.run([
                        'docker', 
                        'build', 
                        '-f',
                        dockerfile_path,
                        '-t', 
                        config['project']['docker_image'],
                        '.'
                    ], 
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        
        if not success:
            return success, err
        
        # Push docker image
        process = subprocess.run([
                        'docker', 
                        'push', 
                        config['project']['docker_image'],
                    ], 
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        
        return success, err
    
    run_progress_step("Preparing deployment", prepare_deployment)
    
    # Create/save a k8s manifest
    job_id = f"{config['project']['name']}-{int(time.time())}"
    job_dir = os.path.join(config_dir, "jobs", job_id)
    os.makedirs(job_dir, exist_ok=True)
    
    job_spec_filepath = os.path.join(job_dir, "elasticjob.yaml")
    
    job_k8s_spec = prepare_torchelastic_job_k8s_spec(
        job_id=job_id, 
        image_uri=config['project']['docker_image'], 
        entrypoint=entrypoint, 
        min_num_nodes=min_nodes, max_num_nodes=max_nodes, 
        nodegroup=ng, 
        resources=INSTANCE_TYPE_X_RESOURCES[instance_type]
    )
    
    with open(job_spec_filepath, "w") as f:
        yaml.dump(job_k8s_spec, f)

    # Apply the k8s manifest
    def apply_job_k8s_manifest():
        process = subprocess.run([
                'kubectl', 
                'apply',
                '-f',
                job_spec_filepath,
            ], 
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        return success, err
        
    run_progress_step("Deploying job to cluster", apply_job_k8s_manifest)
    
    print()
    print(f"Job id: {job_id}")
    print("-"*15)
    print(f"To stop this job, run the following command: mlbot stop {job_id}")
    
    
@cli.command()
@click.argument('job_id', nargs=1, type=str, required=True)
def stop(job_id):
    # Delete the TorchElastic k8s job corresponding to this given id
    def stop_job():
        process = subprocess.run([
            'kubectl', 
            'delete',
            '-n',
            'elastic-job',
            f"elasticjob.elastic.pytorch.org/{job_id}",
        ], 
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True)
        success = process.returncode == 0
        err = process.stderr
        return success, err
    
    run_progress_step("Stopping job", stop_job)

@cli.command()
def delete_cluster():
    iam_client = boto3.client('iam')
    
    config_dir = os.path.join(os.getcwd(), CONFIG_FOLDER_NAME)
    config_filepath = os.path.join(config_dir, "config.json")
    
    try:
        config = json.load(open(config_filepath))
        assert is_valid_config(config, validate_project_config=False)
    except:
        print("MLbot isn't properly configured for this project yet. To do this, first run 'mlbot init' before running this command.")
        sys.exit(1)

    # Confirm with the user that they *really* want to delete this cluster
    confirmation = input(f"WARNING: This will DELETE your EKS cluster (but NOT the {CONFIG_FOLDER_NAME} folder). Are you sure you want to proceed? [y/N]: ")
    confirmation = confirmation.lower().strip()
    
    if not confirmation or confirmation == "n":
        print("Your cluster was NOT deleted.")
        sys.exit(1)
        
    def delete_eks_cluster():
        subprocess.check_call([
            'eksctl', 
            'delete',
            'cluster',
            '--region',
            config['cluster']['region'],
            '--name',
            config['cluster']['name'],
        ], stdout=sys.stdout, stderr=subprocess.STDOUT)
            
        cluster_autoscaler_policy_arn = config['cluster'].get('autoscaler_policy_arn')
        if cluster_autoscaler_policy_arn:
            iam_client.delete_policy(
                PolicyArn=cluster_autoscaler_policy_arn,
            )
    
    print("Deleting EKS cluster...\n\n")
    delete_eks_cluster()
    print()
    print(f"Note: The {CONFIG_FOLDER_NAME} directory was NOT deleted, but be sure to run 'mlbot create-cluster' before launching any other jobs.")

    
if __name__ == "__main__":
    cli()