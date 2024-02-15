"""Contains the definition of the base Ax generator using the service API."""

from typing import List, Optional, Dict
import os

import numpy as np
import torch
from packaging import version
from ax.version import version as ax_version
from ax.core.types import TParameterization
from ax.core.observation import ObservationFeatures
from ax.service.utils.instantiation import (
    InstantiationBase,
    ObjectiveProperties,
)
from ax.modelbridge.registry import Models
from ax.modelbridge.generation_strategy import (
    GenerationStep,
    GenerationStrategy,
)
from ax.utils.notebook.plotting import render
from ax.plot.contour import plot_contour
from ax.plot.diagnostic import interact_cross_validation
from ax.plot.slice import plot_slice
from ax.plot.feature_importances import plot_feature_importance_by_feature
from ax.modelbridge.cross_validation import cross_validate


from optimas.utils.logger import get_logger
from optimas.utils.other import update_object
from optimas.core import (
    Objective,
    Trial,
    VaryingParameter,
    Parameter,
    TrialStatus,
)
from optimas.generators.ax.base import AxGenerator
from optimas.generators.base import Generator
from .custom_ax import CustomAxClient as AxClient

logger = get_logger(__name__)


class AxServiceGenerator(AxGenerator):
    """Base class for all Ax generators using the service API.

    Parameters
    ----------
    varying_parameters : list of VaryingParameter
        List of input parameters to vary.
    objectives : list of Objective
        List of optimization objectives.
    analyzed_parameters : list of Parameter, optional
        List of parameters to analyze at each trial, but which are not
        optimization objectives. By default ``None``.
    parameter_constraints : list of str, optional
        List of string representation of parameter
        constraints, such as ``"x3 >= x4"`` or ``"-x3 + 2*x4 - 3.5*x5 >= 2"``.
        For the latter constraints, any number of arguments is accepted, and
        acceptable operators are ``<=`` and ``>=``.
    outcome_constraints : list of str, optional
        List of string representation of outcome constraints (i.e., constraints
        on any of the ``analyzed_parameters``) of form
        ``"metric_name >= bound"``, like ``"m1 <= 3."``.
    n_init : int, optional
        Number of evaluations to perform during the initialization phase using
        Sobol sampling. If external data is attached to the exploration, the
        number of initialization evaluations will be reduced by the same
        amount, unless `enforce_n_init=True`. By default, ``4``.
    enforce_n_init : bool, optional
        Whether to enforce the generation of `n_init` Sobol trials, even if
        external data is supplied. By default, ``False``.
    abandon_failed_trials : bool, optional
        Whether failed trials should be abandoned (i.e., not suggested again).
        By default, ``True``.
    fit_out_of_design : bool, optional
        Whether to fit the surrogate model taking into account evaluations
        outside of the range of the varying parameters. This can be useful
        if the range of parameter has been reduced during the optimization.
        By default, False.
    use_cuda : bool, optional
        Whether to allow the generator to run on a CUDA GPU. By default
        ``False``.
    gpu_id : int, optional
        The ID of the GPU in which to run the generator. By default, ``0``.
    dedicated_resources : bool, optional
        Whether to allocated dedicated resources (e.g., the GPU) for the
        generator. These resources will not be available to the
        simulation workers. By default, ``False``.
    save_model : bool, optional
        Whether to save the optimization model (in this case, the Ax client) to
        disk. By default ``True``.
    model_save_period : int, optional
        Periodicity, in number of evaluated Trials, with which to save the
        model to disk. By default, ``5``.
    model_history_dir : str, optional
        Name of the directory in which the model will be saved. By default,
        ``'model_history'``.

    """

    def __init__(
        self,
        varying_parameters: List[VaryingParameter],
        objectives: List[Objective],
        analyzed_parameters: Optional[List[Parameter]] = None,
        parameter_constraints: Optional[List[str]] = None,
        outcome_constraints: Optional[List[str]] = None,
        n_init: Optional[int] = 4,
        enforce_n_init: Optional[bool] = False,
        abandon_failed_trials: Optional[bool] = True,
        fit_out_of_design: Optional[bool] = False,
        use_cuda: Optional[bool] = False,
        gpu_id: Optional[int] = 0,
        dedicated_resources: Optional[bool] = False,
        save_model: Optional[bool] = True,
        model_save_period: Optional[int] = 5,
        model_history_dir: Optional[str] = "model_history",
    ) -> None:
        super().__init__(
            varying_parameters=varying_parameters,
            objectives=objectives,
            analyzed_parameters=analyzed_parameters,
            use_cuda=use_cuda,
            gpu_id=gpu_id,
            dedicated_resources=dedicated_resources,
            save_model=save_model,
            model_save_period=model_save_period,
            model_history_dir=model_history_dir,
            allow_fixed_parameters=True,
            allow_updating_parameters=True,
        )
        self._n_init = n_init
        self._enforce_n_init = enforce_n_init
        self._abandon_failed_trials = abandon_failed_trials
        self._fit_out_of_design = fit_out_of_design
        self._fixed_features = None
        self._parameter_constraints = parameter_constraints
        self._outcome_constraints = outcome_constraints
        self._ax_client = self._create_ax_client()

    @property
    def ax_client(self) -> AxClient:
        """Get the underlying AxClient."""
        return self._ax_client

    def _ask(self, trials: List[Trial]) -> List[Trial]:
        """Fill in the parameter values of the requested trials."""
        for trial in trials:
            try:
                parameters, trial_id = self._ax_client.get_next_trial(
                    fixed_features=self._fixed_features
                )
            # Occurs when not using a CustomAxClient (i.e., when the AxClient
            # is provided by the user using an AxClientGenerator). In that
            # case, there is also no need to support FixedFeatures.
            except TypeError:
                parameters, trial_id = self._ax_client.get_next_trial()
            trial.parameter_values = [
                parameters.get(var.name) for var in self._varying_parameters
            ]
            trial.ax_trial_id = trial_id
        return trials

    def _tell(self, trials: List[Trial]) -> None:
        """Incorporate evaluated trials into Ax client."""
        for trial in trials:
            try:
                trial_id = trial.ax_trial_id
                ax_trial = self._ax_client.get_trial(trial_id)
            except AttributeError:
                params = {}
                for var, value in zip(
                    trial.varying_parameters, trial.parameter_values
                ):
                    params[var.name] = value
                _, trial_id = self._ax_client.attach_trial(params)
                ax_trial = self._ax_client.get_trial(trial_id)

                # Since data was given externally, reduce number of
                # initialization trials, but only if they have not failed.
                if (
                    trial.status != TrialStatus.FAILED
                    and not self._enforce_n_init
                ):
                    gs = self._ax_client.generation_strategy
                    cs = gs.current_step
                    if version.parse(ax_version) >= version.parse("0.3.5"):
                        cs = gs.current_step
                        # Reduce only if there are still Sobol trials left.
                        if gs.current_step.model == Models.SOBOL:
                            cs.num_trials -= 1
                            cs.transition_criteria[0].threshold -= 1
                            gs._maybe_move_to_next_step()
                    else:
                        # Reduce only if there are still Sobol trials left.
                        if gs.current_step.model == Models.SOBOL:
                            gs.current_step.num_trials -= 1
                            gs._maybe_move_to_next_step()
            finally:
                if trial.completed:
                    outcome_evals = {}
                    # Add objective evaluations.
                    for ev in trial.objective_evaluations:
                        outcome_evals[ev.parameter.name] = (ev.value, ev.sem)
                    # Add outcome constraints evaluations.
                    ax_config = self._ax_client.experiment.optimization_config
                    if ax_config.outcome_constraints:
                        ocs = [
                            oc.metric.name
                            for oc in ax_config.outcome_constraints
                        ]
                        for ev in trial.parameter_evaluations:
                            par_name = ev.parameter.name
                            if par_name in ocs:
                                outcome_evals[par_name] = (ev.value, ev.sem)
                    self._ax_client.complete_trial(
                        trial_index=trial_id, raw_data=outcome_evals
                    )
                elif trial.failed:
                    if self._abandon_failed_trials:
                        ax_trial.mark_abandoned()
                    else:
                        ax_trial.mark_failed()

    def _create_ax_client(self) -> AxClient:
        """Create Ax client."""
        bo_model_kwargs = {
            "torch_dtype": torch.double,
            "torch_device": torch.device(self.torch_device),
            "fit_out_of_design": self._fit_out_of_design,
        }
        ax_client = AxClient(
            generation_strategy=GenerationStrategy(
                self._create_generation_steps(bo_model_kwargs)
            ),
            verbose_logging=False,
        )
        ax_client.create_experiment(
            parameters=self._create_ax_parameters(),
            objectives=self._create_ax_objectives(),
            outcome_constraints=self._outcome_constraints,
            parameter_constraints=self._parameter_constraints,
        )
        return ax_client

    def _create_ax_parameters(self) -> List:
        """Create list of parameters to pass to an Ax."""
        parameters = []
        fixed_parameters = {}
        for var in self._varying_parameters:
            # Determine parameter type.
            value_dtype = np.dtype(var.dtype)
            if value_dtype.kind == "f":
                value_type = "float"
            elif value_dtype.kind == "i":
                value_type = "int"
            else:
                raise ValueError(
                    "Ax range parameter can only be of type 'float'ot 'int', "
                    "not {var.dtype}."
                )
            # Create parameter dict and append to list.
            parameters.append(
                {
                    "name": var.name,
                    "type": "range",
                    "bounds": [var.lower_bound, var.upper_bound],
                    "is_fidelity": var.is_fidelity,
                    "target_value": var.fidelity_target_value,
                    "value_type": value_type,
                }
            )
            if var.is_fixed:
                fixed_parameters[var.name] = var.default_value
        # Store fixed parameters as fixed features.
        self._fixed_features = ObservationFeatures(fixed_parameters)
        return parameters

    def _create_ax_objectives(self) -> Dict[str, ObjectiveProperties]:
        """Create list of objectives to pass to an Ax."""
        objectives = {}
        for obj in self.objectives:
            objectives[obj.name] = ObjectiveProperties(minimize=obj.minimize)
        return objectives

    def _create_generation_steps(
        self, bo_model_kwargs: Dict
    ) -> List[GenerationStep]:
        """Create generation steps (must be implemented by subclasses)."""
        raise NotImplementedError

    def _save_model_to_file(self) -> None:
        """Save Ax client to json file."""
        file_path = os.path.join(
            self._model_history_dir,
            "ax_client_at_eval_{}.json".format(
                self._n_evaluated_trials_last_saved
            ),
        )
        self._ax_client.save_to_json_file(file_path)

    def _prepare_to_send(self) -> None:
        """Prepare generator to send to another process.

        Delete the fitted model from the generation strategy. It can contain
        pytorch tensors that prevent serialization.
        """
        generation_strategy = self._ax_client.generation_strategy
        if generation_strategy._model is not None:
            del generation_strategy._curr.model_spec._fitted_model
            generation_strategy._curr.model_spec._fitted_model = None
            del generation_strategy._model
            generation_strategy._model = None

    def _update(self, new_generator: Generator) -> None:
        """Update generator with the attributes of a newer one.

        This method is overrides the base one to make sure that the original
        AxClient is updated and not simply replaced.

        Parameters
        ----------
        new_generator : Generator
            The newer version of the generator returned in ``persis_info``.

        """
        original_ax_client = self._ax_client
        super()._update(new_generator)
        update_object(original_ax_client, new_generator._ax_client)
        self._ax_client = original_ax_client

    def _update_parameter(self, parameter: VaryingParameter) -> None:
        """Update a parameter from the search space."""
        parameters = self._create_ax_parameters()
        new_search_space = InstantiationBase.make_search_space(parameters, None)
        self._ax_client.experiment.search_space.update_parameter(
            new_search_space.parameters[parameter.name]
        )

    def _mark_trial_as_failed(self, trial: Trial):
        """Mark a trial as failed so that is not used for fitting the model."""
        ax_trial = self._ax_client.get_trial(trial.ax_trial_id)
        if self._abandon_failed_trials:
            ax_trial.mark_abandoned(unsafe=True)
        else:
            ax_trial.mark_failed(unsafe=True)

    def _get_best_values(self, objective: str) -> Optional[TParameterization]:
        """Get the values of the best predicted parametrization."""
        objective_names = [obj.name for obj in self.objectives]
        if objective in objective_names:
            objective = self.objectives[objective_names.index(objective)]
        else:
            raise ValueError(
                f"Objective {objective} not found. Available objectives "
                f"are {objective_names}."
            )

        if len(self.objectives) > 1:
            pp = self._ax_client.get_pareto_optimal_parameters()
            obj_vals = [
                objs[objective.name] for i, (vals, (objs, covs)) in pp.items()
            ]
            param_vals = [vals for i, (vals, (objs, covs)) in pp.items()]
            if objective.minimize:
                best_obj_i = np.argmin(obj_vals)
            else:
                best_obj_i = np.argmax(obj_vals)
            slice_values = param_vals[best_obj_i]

        else:
            result = self._ax_client.get_best_parameters()
            if result is not None:
                slice_values, _ = result
            else:
                slice_values = None
        return slice_values

    def plot_contour(
        self,
        param_x: Optional[str] = None,
        param_y: Optional[str] = None,
        objective: Optional[str] = None,
        slice_values: Optional[Dict] = None,
    ) -> None:
        """Plot a 2D slice of the surrogate model.

        Parameters
        ----------
        param_x : str, optional
            Name of the varying parameter sliced on the x axis. If not given,
            the first varying parameter will used. By default ``None``.
        param_y : str, optional
            Name of the varying parameter sliced on the y axis. If not given,
            the second varying parameter will used. By default ``None``.
        objective : str, optional
            Name of the objective to plot. If not given, the first objective
            will be shown. By default ``None``.
        slice_values : Optional[Dict], optional
            A dictionary ``{name: val}`` for the fixed values of the other
            parameters. If not provided, then the values of the best predicted
            parametrization will be used. By default ``None``.
        """
        if len(self.varying_parameters) < 2:
            raise ValueError(
                "Cannot plot contour because there are less than 2 varying "
                "parameters."
            )
        if param_x is None:
            param_x = self.varying_parameters[0].name
        if param_y is None:
            param_y = self.varying_parameters[1].name
        if objective is None:
            objective = self.objectives[0].name
        self._ax_client.fit_model()
        if slice_values is None:
            slice_values = self._get_best_values(objective)
        if slice_values is None:
            logger.info(
                "Could not determine best parameters. The 2D countour will be "
                "a slice along the mean of the range of the varying parameters."
            )
        render(
            plot_contour(
                model=self._ax_client.generation_strategy.model,
                param_x=param_x,
                param_y=param_y,
                metric_name=objective,
                slice_values=slice_values,
            )
        )

    def plot_cross_validation(self) -> None:
        """Show an interactive cross-validation plot."""
        self._ax_client.fit_model()
        render(
            interact_cross_validation(
                cross_validate(model=self._ax_client.generation_strategy.model)
            )
        )

    def plot_feature_importance(self) -> None:
        """Plot the importance of the input parameters.

        The feature importance score represents how useful each parameter
        is for predicting the model outcome.
        """
        self._ax_client.fit_model()
        render(
            plot_feature_importance_by_feature(
                model=self._ax_client.generation_strategy.model
            )
        )

    def plot_slice(
        self,
        param: Optional[str] = None,
        objective: Optional[str] = None,
        slice_values: Optional[Dict] = None,
    ) -> None:
        """Plot a 1D slide of the surrogate model.

        Parameters
        ----------
        param : str, optional
            Name of the varying parameter sliced on the x axis. If not given,
            the first varying parameter will used. By default ``None``.
        objective : str, optional
            Name of the objective to plot. If not given, the first objective
            will be shown. By default ``None``.
        slice_values : dict, optional
            A dictionary ``{name: val}`` for the fixed values of the other
            parameters. If not provided, then the values of the best predicted
            parametrization will be used. By default ``None``.
        """
        if param is None:
            param = self.varying_parameters[0].name
        if objective is None:
            objective = self.objectives[0].name
        self._ax_client.fit_model()
        if slice_values is None:
            slice_values = self._get_best_values(objective)
        if slice_values is None:
            logger.info(
                "Could not determine best parameters. The model will be "
                "sliced along the mean of the range of the varying parameters."
            )
        render(
            plot_slice(
                model=self._ax_client.generation_strategy.model,
                param_name=param,
                metric_name=objective,
                slice_values=slice_values,
            )
        )
