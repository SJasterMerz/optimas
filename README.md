# Optimization of PIC simulation with libEnsemble

The scripts in this repository allow to optimize PIC simulation.

## Installing

### On a local computer

Then install other dependencies:
```
pip install libensemble
pip install -r requirements.txt
```

Then
```
git clone https://github.com/optimas-org/optimas.git
cd 
pip install .
```

### On Summit

In the instructions below, before the `git clone` command, `cd` into your `$MEMBERWORK` folder, and create a dedicated directory.

#### For FBPIC simulations

Install according to:
https://fbpic.github.io/install/install_summit.html

Then install other dependencies:
```
source activate $SCRATCH/fbpic_env
pip install libensemble
git clone https://github.com/optimas-org/optimas.git
cd 
pip install .
source deactivate
```

#### For WarpX simulations
```
conda create -n 
source activate 
conda install -c conda-forge mamba
mamba install -c conda-forge openpmd-viewer openpmd-api pandas botorch ax-platform
pip install libensemble
git clone https://github.com/optimas-org/optimas.git
cd 
pip install .
```

### On Lawrencium

Install according to:
https://fbpic.github.io/install/install_lawrencium.html

Then install other dependencies:
```
pip install libensemble
pip install -r requirements.txt
```

`cd` into your `$SCRATCH` folder, and create a dedicated directory. Then run:
```
git clone https://github.com/optimas-org/optimas.git
cd 
pip install .
```

## Usage

`cd` into the folder `optimization_folder`, and run the script
`create_new_optimization.py`. (In order to see the usage of this script,
type `python create_new_optimization.py -h`.) Then follow the printed instructions.

Note that the script will create a new folder, with a number of files,
which you can modify before submitting/launching the optimization job:

- `template_simulation_script.py`: an simulation script, templated with `jinja2` syntax.
- `varying_parameters.py`: list of varying parameters, along with their bounds. These variables should match the templated variables in `template_simulation_script.py`.
- `mf_parameters.py` (optional): defines fidelity parameters for multi-fidelity optimization.
- `analysis_script.py`: analyzes the result of the simulation and extract the objective function `f`.

You can also resume a previous `libE` optimization (of the exact same problem), by copying the `.npy` file that was produced by `libE` into the new folder, and renaming it as `past_history.npy`.

You can easily postprocess the optimization in a Jupyter notebook by using:
```
from optimas.post_processing import PostProcOptimization
pp = PostProcOptimization('path/to/your/optimization')
```
