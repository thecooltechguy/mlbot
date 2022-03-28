## Quickstart
Be sure to replace some of the command-line option values below with ones specific to your environment setup (e.g., instance type, availability zone, etc.).

1. `mlbot init --project_name pl-mnist-example --docker_image <the docker image name to use for this project>`
2. `mlbot run --instance-type p3dn.24xlarge --az us-west-2b --num-nodes 1 train.py --trainer.gpus 8 --trainer.num_nodes 1 --trainer.strategy 'ddp' --trainer.max_epochs 1000`
3. Once the job is running, you can view live logs by running: `kubectl attach -n elastic-job <project id>-worker-0`

### Source
This example is adapted from the <a href="https://github.com/PyTorchLightning/pytorch-lightning/tree/master/pl_examples/basic_examples">PyTorch Lightning repo</a>.