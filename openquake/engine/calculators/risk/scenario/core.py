# Copyright (c) 2010-2013, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.


"""
Core functionality for the scenario risk calculator.
"""
import numpy
from django import db

from openquake.risklib import api, scientific

from openquake.engine import logs
from openquake.engine.calculators import base
from openquake.engine.calculators.risk import general
from openquake.engine.utils import tasks, stats
from openquake.engine.db import models


@tasks.oqtask
@stats.count_progress('r')
def scenario(job_id, hazard, seed, vulnerability_function, output_containers,
             insured_losses, asset_correlation):
    """
    Celery task for the scenario damage risk calculator.

    :param job_id: the id of the current
    :class:`openquake.engine.db.models.OqJob`
    :param dict hazard:
      A dictionary mapping IDs of
      :class:`openquake.engine.db.models.Output` (with output_type set
      to 'gmfscenario') to a tuple where the first element is an instance of
      :class:`..hazard_getters.GroundMotionScenarioGetter2`, and the second
      element is the corresponding weight.
    :param seed: the seed used to initialize the rng
    :param output_containers: a dictionary {hazard_id: output_id}
        where output id represents the id of the loss map
    :param bool insured_losses: True if also insured losses should be computed
    :param asset_correlation: asset correlation coefficient
    """

    calc = api.Scenario(vulnerability_function, seed, asset_correlation)

    hazard_getter = hazard.values()[0][0]

    assets, ground_motion_values, missings = hazard_getter()

    with logs.tracing('computing risk'):
        loss_ratio_matrix = calc(ground_motion_values)

        if insured_losses:
            insured_loss_ratio_matrix = [
                scientific.insured_losses(
                    loss_ratio_matrix[i], asset.value,
                    asset.deductible, asset.ins_limit)
                for i, asset in enumerate(assets)]

    # There is only one output container list as there is no support
    # for hazard logic tree
    output_containers = output_containers.values()[0]

    loss_map_id = output_containers[0]

    if insured_losses:
        insured_loss_map_id = output_containers[1]

    with db.transaction.commit_on_success(using='reslt_writer'):
        for i, asset in enumerate(assets):
            general.write_loss_map_data(
                loss_map_id, asset,
                loss_ratio_matrix[i].mean(),
                std_dev=loss_ratio_matrix[i].std(ddof=1))

            if insured_losses:
                general.write_loss_map_data(
                    insured_loss_map_id, asset,
                    insured_loss_ratio_matrix[i].mean(),
                    std_dev=insured_loss_ratio_matrix[i].std(ddof=1))

    aggregate_losses = sum(loss_ratio_matrix[i] * asset.value
                           for i, asset in enumerate(assets))

    if insured_losses:
        insured_aggregate_losses = sum(
            insured_loss_ratio_matrix[i] * asset.value
            for i, asset in enumerate(assets))
    else:
        insured_aggregate_losses = "Not computed"

    base.signal_task_complete(
        job_id=job_id,
        num_items=len(assets) + len(missings),
        aggregate_losses=aggregate_losses,
        insured_aggregate_losses=insured_aggregate_losses)
scenario.ignore_result = False


class ScenarioRiskCalculator(general.BaseRiskCalculator):
    """
    Scenario Risk Calculator. Computes a Loss Map,
    for a given set of assets.
    """
    hazard_getter = general.hazard_getters.GroundMotionScenarioGetter

    core_calc_task = scenario

    def __init__(self, job):
        super(ScenarioRiskCalculator, self).__init__(job)
        self.aggregate_losses = None
        self.insured_aggregate_losses = None

    def pre_execute(self):
        """
        Override the default pre_execute to provide more detailed
        validation.

        2) If insured losses are required we check for the presence of
        the deductible and insurance limit
        """
        super(ScenarioRiskCalculator, self).pre_execute()

        if (self.rc.insured_losses and
            self.exposure_model.exposuredata_set.filter(
                (db.models.Q(deductible__isnull=True) |
                 db.models.Q(ins_limit__isnull=True))).exists()):
            raise RuntimeError(
                "Deductible or insured limit missing in exposure")

    def task_completed_hook(self, message):
        aggregate_losses = message['aggregate_losses']

        if self.aggregate_losses is None:
            self.aggregate_losses = numpy.zeros(aggregate_losses.shape)
        self.aggregate_losses += aggregate_losses

        if self.rc.insured_losses:
            insured_aggregate_losses = message['insured_aggregate_losses']

            if self.insured_aggregate_losses is None:
                self.insured_aggregate_losses = numpy.zeros(
                    insured_aggregate_losses.shape)
            self.insured_aggregate_losses += insured_aggregate_losses

    def post_process(self):
        with db.transaction.commit_on_success(using='reslt_writer'):
            models.AggregateLossData.objects.create(
                output=models.Output.objects.create_output(
                    self.job, "Aggregate Loss",
                    "aggregate_loss"),
                mean=numpy.mean(self.aggregate_losses),
                std_dev=numpy.std(self.aggregate_losses, ddof=1))

            if self.rc.insured_losses:
                models.AggregateLossData.objects.create(
                    output=models.Output.objects.create_output(
                        self.job, "Insured Aggregate Loss",
                        "aggregate_loss"),
                    mean=numpy.mean(self.insured_aggregate_losses),
                    std_dev=numpy.std(self.insured_aggregate_losses, ddof=1))

    def hazard_outputs(self, hazard_calculation):
        """
        :returns: the single hazard output associated to
        `hazard_calculation`
        """

        # in scenario hazard calculation we do not have hazard logic
        # tree realizations, and we have only one output
        return hazard_calculation.oqjob_set.filter(status="complete").latest(
            'last_update').output_set.get(
                output_type='gmf_scenario')

    def create_getter(self, output, assets):
        """
        See :method:`..general.BaseRiskCalculator.create_getter`
        """
        if output.output_type != 'gmf_scenario':
            raise RuntimeError(
                "The provided hazard output is not a ground motion field: %s"
                % output.output_type)
        return (self.hazard_getter(
            output.id, self.imt, assets,
            self.rc.get_hazard_maximum_distance()),
            1)

    @property
    def calculator_parameters(self):
        """
        Provides calculator specific params coming from
        :class:`openquake.engine.db.RiskCalculation`
        """

        return [self.rc.insured_losses, self.rc.asset_correlation or 0]

    def create_outputs(self, hazard_output):
        """
        Create the the output of a ScenarioRisk calculator
        which is a LossMap.
        """

        if self.rc.insured_losses:
            insured_loss_map = [models.LossMap.objects.create(
                output=models.Output.objects.create_output(
                    self.job, "Insured Loss Map", "loss_map"),
                hazard_output=hazard_output).id]
        else:
            insured_loss_map = []

        return [models.LossMap.objects.create(
                output=models.Output.objects.create_output(
                    self.job, "Loss Map", "loss_map"),
                hazard_output=hazard_output).id] + insured_loss_map
