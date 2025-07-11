# XProf (+ Tensorboard Profiler Plugin)
XProf includes a suite of tools for [JAX](https://jax.readthedocs.io/), [TensorFlow](https://www.tensorflow.org/), and [PyTorch/XLA](https://github.com/pytorch/xla). These tools help you understand, debug and optimize programs to run on CPUs, GPUs and TPUs.

XProf offers a number of tools to analyse and visualize the
performance of your model across multiple devices. Some of the tools include:

*   **Overview**: A high-level overview of the performance of your model. This
    is an aggregated overview for your host and all devices. It includes:
    *   Performance summary and breakdown of step times.
    *   A graph of individual step times.
    *   A table of the top 10 most expensive operations.
*   **Trace Viewer**: Displays a timeline of the execution of your model that shows:
    *   The duration of each op.
    *   Which part of the system (host or device) executed an op.
    *   The communication between devices.
*   **Memory Profile Viewer**: Monitors the memory usage of your model.
*   **Graph Viewer**: A visualization of the graph structure of HLOs of your model.

## Demo
First time user? Come and check out this [Colab Demo](https://docs.jaxstack.ai/en/latest/JAX_for_LLM_pretraining.html).

## Prerequisites

* tensorboard-plugin-profile >= 2.19.0
* (optional) TensorBoard >= 2.19.0

Note: XProf requires access to the Internet to load the [Google Chart library](https://developers.google.com/chart/interactive/docs/basic_load_libs#basic-library-loading).
Some charts and tables may be missing if you run TensorBoard entirely offline on
your local machine, behind a corporate firewall, or in a datacenter.

To profile on a **single GPU** system, the following NVIDIA software must be
installed on your system:

1. NVIDIA GPU drivers and CUDA Toolkit:
    * CUDA 12.5 requires 525.60.13 and higher.
2. Ensure that CUPTI 10.1 exists on the path.

   ```shell
   $ /sbin/ldconfig -N -v $(sed 's/:/ /g' <<< $LD_LIBRARY_PATH) | grep libcupti
   ```

   If you don't see `libcupti.so.12.5` on the path, prepend its installation
   directory to the $LD_LIBRARY_PATH environmental variable:

   ```shell
   $ export LD_LIBRARY_PATH=/usr/local/cuda/extras/CUPTI/lib64:$LD_LIBRARY_PATH
   ```
   Run the ldconfig command above again to verify that the CUPTI 12.5 library is
   found.

   If this doesn't work, try:
   ```shell
   $ sudo apt-get install libcupti-dev
   ```

To profile a system with **multiple GPUs**, see this [guide](https://github.com/tensorflow/profiler/blob/master/docs/profile_multi_gpu.md) for details.

To profile multi-worker GPU configurations, profile individual workers
independently.

To profile cloud TPUs, you must have access to Google Cloud TPUs.

## Quick Start
In order to get the latest version of the profiler plugin, you can install the
nightly package.

To install the nightly version of profiler:

```
$ pip uninstall xprof
$ pip install xprof-nightly
```

Without TensorBoard:
```
$ xprof --logdir=profiler/demo --port=6006
```

With TensorBoard:

```
$ tensorboard --logdir=profiler/demo
```
If you are behind a corporate firewall, you may need to include the `--bind_all`
tensorboard flag.

Go to `localhost:6006/#profile` of your browser, you should now see the demo
overview page show up.
Congratulations! You're now ready to capture a profile.

## Next Steps

* JAX Profiling Guide: https://jax.readthedocs.io/en/latest/profiling.html
* TensorFlow Profiling Guide: https://tensorflow.org/guide/profiler
* Cloud TPU Profiling Guide: https://cloud.google.com/tpu/docs/cloud-tpu-tools
* Colab Tutorial: https://www.tensorflow.org/tensorboard/tensorboard_profiling_keras
* Tensorflow Colab: https://www.tensorflow.org/tensorboard/tensorboard_profiling_keras
