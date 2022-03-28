# ![MLbot](./logo.svg)
**MLbot** offers a fast & easy way to train ML models in your cloud, directly from your laptop.

It does this by abstracting away all the complexities of setting up a compute cluster & launching distributed training jobs, behind a few simple commands.

So, as a data scientist, running your training code across multiple machines in your own cloud becomes as easy as replacing `python` with `mlbot run` (e.g., `python train.py ...` &rarr; `mlbot run --instance-type p3dn.24xlarge --num-nodes 2 train.py ...`).

Everything else -- from packaging/deploying your code to running your job in a fault-tolerant way across multiple nodes -- gets automatically taken care of for you.

## How does this work?
On a high level, MLbot glues together Kubernetes & PyTorch Elastic to enable you to easily launch distributed training jobs in your own infrastructure (as opposed to using a hosted service).

This way, you don't need to transfer your data to a 3rd-party, while having full flexibility & observability over the underlying compute.

## Dependencies
Currently, **MLbot** depends on the following tools:
- <a href="https://eksctl.io/">eksctl</a>
- <a href="https://docs.aws.amazon.com/eks/latest/userguide/install-kubectl.html">kubectl (for EKS)</a>

## Installation
`pip install mlbot-cloud`

## Quick-start
### 1. Get the MNIST training example
I've included a sample MNIST training script in this repo (adapted from <a href="https://github.com/PyTorchLightning/pytorch-lightning/tree/master/pl_examples/basic_examples">an example provided by PyTorch Lightning</a>) to help you try this tool out quickly.

To get started, run the following commands:

1. `git clone https://github.com/thecooltechguy/mlbot.git`
2. `cd mlbot/examples/pytorch-lightning-image-classifier/` 

### 2. Create a new compute cluster (~15 mins, one-time setup)
For this example, we'll create an AWS Elastic Kubernetes Service (EKS) cluster (running k8s version `1.21`) in the `us-west-2` region to run our training jobs on:

```mlbot create-cluster --cluster testcluster --version 1.21 --region us-west-2 --availability-zones us-west-2a,us-west-2b,us-west-2c --instance-types p3dn.24xlarge --max-gpu-nodes 2 --create```

This command will:
1. Create a new `.mlbot` folder in the current directory
2. Generate a new cluster configuration file for `eksctl` and save this to `.mlbot/cluster.yaml` 
3. Generate a new cluster configuration file for `mlbot` and save this to `.mlbot/cluster.json`
4. Call `eksctl` to create a new EKS cluster using the new cluster configuration file

<details>
  <summary>More info</summary>
 
If you'd like to *only* create the cluster configuration file and then separately create the EKS cluster using this file, you can run the same command as shown above, but remove the final `--create` flag.

You can then separately run `eksctl create cluster -f .mlbot/cluster.yaml` to manually create the EKS cluster. This can be useful if you would like to edit the file before creating the EKS cluster.
</details>

### 3. Setup the compute cluster (<5 mins, one-time setup)

```mlbot setup-cluster```

This command will make our EKS cluster ready for running distributed PyTorch jobs (e.g., by installing the cluster autoscaler, metrics server, etc.).

Once the above command is done, let's scale up a single non-GPU node in our cluster to run all pending pods:

```eksctl scale nodegroup --cluster=testcluster --nodes=1 standard-ng-1```

### 4. Run an example training job
Now that our cluster is ready, we can now run distributed training jobs on it!

First, we need to specify the project name and docker image name that MLbot should use for deploying this project's compute jobs. The project name will be used as the prefix for all of this project's jobs, while the docker image name will be used for the images that MLbot builds & pushes for packaging your code.

```mlbot init --project-name pl-mnist-example --docker-image <the docker image name to use for this project>```

**Note:** This command will update the project's `.mlbot/config.json` file, and only needs to be run once per project.
	
Now that we've configured our project, we can finally run our training script in the cloud with the following command:

```mlbot run --instance-type p3dn.24xlarge --az us-west-2b --num-nodes 2 train.py --trainer.gpus 8 --trainer.num_nodes 2 --trainer.strategy 'ddp' --trainer.max_epochs 100```

The example command shown above will launch our training job across 2 `p3dn.24xlarge` instances (with a total of 16 GPUs).

Note that everything before `train.py` in the above command is basically what's replacing `python`, if you were to run this training script locally.

Once the job is running, you can view its live logs by running: `kubectl attach -n elastic-job <project id>-worker-0`. Similarly, to view the live logs of the second node, you can run: `kubectl attach -n elastic-job <project id>-worker-1`.

To view the logs of all training nodes, a better way might be to use a tool like <a href="https://github.com/stern/stern">Stern</a> and run `stern -n elastic-job <job id> -t`

To stop this job, run: `mlbot stop <job id>`. This will delete the compute job from the k8s cluster.

### 5. Delete the compute cluster
To completely delete the compute cluster we provisioned for this example, simply run:

```mlbot delete-cluster```

This will delete all of the cloud resources that we had provisioned for this example.

## Spot instances
To run your training code on spot instances in a fault-tolerant manner, simply include the `--spot` flag when using `mlbot run` (e.g., `mlbot run --spot --instance-type p3dn.24xlarge --az us-west-2b --num-nodes 2 train.py ...`).

If a spot instance gets reclaimed, the training job will automatically pause and resume once a sufficient number of nodes are available. This applies for jobs with a fixed number of nodes *and* for jobs with an elastic number of nodes.

## Elastic training
To run your training code with a dynamic number of nodes, you can use the `--min-nodes` and `--max-nodes` options instead of a fixed `--num-nodes`, when calling `mlbot run`.

## Using the same cluster for multiple projects
To use the same cluster as project A for project B, you can copy the `cluster.yaml` and `config.json` files from project A's `.mlbot` directory into project B's `.mlbot` directory, and re-run `mlbot init --project ... --docker-image ...` inside project B's directory.

There will definitely be a much better way of doing this soon, as this tool gets updated & refactored.

## Need help?
To learn more about the arguments & options that the various sub-commands support, you can always run the sub-commands with the `--help` flag.

If you have any other questions, please create a GitHub discussion post and I'd be happy to help!

## Limitations
### Using this tool with an existing EKS cluster
I'm currently working on updating the tool so that it can work with existing EKS clusters, instead of requiring users to create a new one. This should be ready soon!

### Supported frameworks & cloud environments
Currently, this tool only supports running distributed PyTorch compute jobs on AWS EKS (Elastic Kubernetes Service). And it should work pretty well with PyTorch Lightning without needing any major code changes (since PyTorch Lightning already supports TorchElastic).

However, support for other frameworks and cloud environments will be added soon, and if you'd like to see a particular integration added, let me know by creating an GitHub discussion post!

## TODO

- Update/refactor code
- Add tests
- Add a better way to view live logs
- Add support for other frameworks & cloud environments

Contributions and feedback are always welcome! :)