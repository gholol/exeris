import math
import sys
from statistics import mean

import random
import sqlalchemy as sql
from exeris.core import deferred, main, util, combat, models, general, properties, recipes
from exeris.core.deferred import convert
from exeris.core.main import db, Events
from exeris.core.properties import P
from flask import logging
from sqlalchemy import func

logger = logging.getLogger(__name__)


class AbstractAction:
    """
    Top-level class in the action system. It represents a serializable in-game operation that leads to a change
    of the game state. It can have a specific executor (e.g. a character dropping an item) or not.

    All classes subclassing AbstactAction should implement method perform_action.
    """

    def perform(self):
        logger.info("Perform %s for arguments: %s", self.__class__.__name__, str(vars(self)))
        return self.perform_action()

    def perform_action(self):
        pass


class PlayerAction(AbstractAction):
    """
    A top-level player action. All we know is that it's done by a player (not their character)
    """

    def __init__(self, player):
        self.player = player


class Action(AbstractAction):
    """
    A top-level character action. All we know is that it's done by a character
    """

    def __init__(self, executor):
        self.executor = executor


# rich collection of pre-configured actions
class ActionOnSelf(Action):
    def __init__(self, executor, rng=None):
        super().__init__(executor)
        self.rng = rng
        if not rng:
            self.rng = general.SameLocationRange()


class ActionOnEntity(Action):
    def __init__(self, executor, entity, rng=None):
        super().__init__(executor)
        self.entity = entity
        if not rng:
            rng = general.SameLocationRange()
        self.rng = rng


class ActionOnItem(ActionOnEntity):
    def __init__(self, executor, item, rng=None):
        super().__init__(executor, item, rng)
        self.item = item


class ActionOnCharacter(ActionOnEntity):
    def __init__(self, executor, character, rng=None):
        super().__init__(executor, character, rng)
        self.character = character


class ActionOnLocation(ActionOnEntity):
    def __init__(self, executor, location, rng=None):
        super().__init__(executor, location, rng)
        self.location = location


class ActionOnActivity(ActionOnEntity):
    def __init__(self, executor, activity, rng=None):
        super().__init__(executor, activity, rng)
        self.activity = activity


class ActionOnItemAndActivity(Action):
    def __init__(self, executor, item, activity, rng=None):
        super().__init__(executor)
        self.item = item
        self.activity = activity
        self.rng = rng
        if not rng:
            self.rng = general.SameLocationRange()


class ActionOnItemAndCharacter(Action):
    def __init__(self, executor, item, character, rng=None):
        super().__init__(executor)
        self.item = item
        self.character = character
        self.rng = rng
        if not rng:
            self.rng = general.SameLocationRange()


####################
# ACTIVITY ACTIONS #
####################

class ActivityAction(AbstractAction):
    pass


def form_on_setup(**kwargs):  # adds a field "_form_input" to a class so it can be later used
    def f(clazz):
        clazz._form_inputs = kwargs
        return clazz

    return f


def set_visible_material(activity, visible_material, entity):
    visible_material_property = {}
    for place_to_show in visible_material:
        group_name = visible_material[place_to_show]
        req_input = activity.requirements["input"]
        for req_material_name, req_material in req_input.items():  # forall requirements
            real_used_type_name = req_material["used_type"]
            if group_name == req_material_name:  # this group is going to be shown by our visible material
                visible_material_property[place_to_show] = real_used_type_name
    entity.properties.append(models.EntityProperty(P.VISIBLE_MATERIAL, visible_material_property))


@form_on_setup(amount=recipes.AmountInput)
class CreateItemAction(ActivityAction):
    @convert(item_type=models.ItemType)
    def __init__(self, *, item_type, properties, used_materials, amount=1, visible_material=None, **injected_args):
        self.item_type = item_type
        self.activity = injected_args["activity"]
        self.initiator = injected_args["initiator"]
        self.amount = amount
        self.used_materials = used_materials
        self.injected_args = injected_args
        self.properties = properties
        self.visible_material = visible_material if visible_material else {}

    def perform_action(self):

        result_loc = self.activity.being_in.being_in

        if self.item_type.portable and self.initiator.being_in == result_loc:  # if being in the same location then go to inventory
            result_loc = self.initiator

        new_items = []
        if self.item_type.stackable:  # create one item with specified 'amount'
            weight = self.amount * self.item_type.unit_weight
            new_items += [self.create_item(result_loc, weight)]
        else:  # create 'amount' of single items
            for _ in range(self.amount):
                new_items += [self.create_item(result_loc, self.item_type.unit_weight)]

        return new_items

    def create_item(self, result_loc, item_weight):
        new_item = models.Item(self.item_type, result_loc, weight=item_weight)
        db.session.add(new_item)
        for property_name in self.properties:
            new_item.properties.append(models.EntityProperty(property_name, self.properties[property_name]))
        if self.used_materials == "all":  # all the materials used for an activity were set to build this item
            for material_type in models.Item.query.filter(models.Item.is_used_for(self.activity)).all():
                material_type.used_for = new_item
        else:  # otherwise it's a dict and we need to look into it
            for material_type_name in self.used_materials:
                self.extract_used_material(material_type_name, new_item)
        if self.visible_material:
            set_visible_material(self.activity, self.visible_material, new_item)
        return new_item

    def extract_used_material(self, material_type_name, new_item):
        for req_material_name, requirement_params in self.activity.requirements.get("input",
                                                                                    {}).items():  # forall requirements
            req_used_type_name = requirement_params["used_type"]
            if req_material_name == material_type_name:  # req is fulfilled by material
                real_material_type = models.ItemType.by_name(req_used_type_name)
                required_material_type = models.EntityType.by_name(req_material_name)

                amount = requirement_params["needed"] / required_material_type.quantity_efficiency(real_material_type)

                item = models.Item.query.filter_by(type=real_material_type).one()
                move_entity_between_entities(item, item.used_for, new_item, amount, to_be_used_for=True)


class CollectGatheredResourcesAction(ActivityAction):
    @convert(resource_type=models.ItemType)
    def __init__(self, *, resource_type, **injected_args):
        self.resource_type = resource_type
        self.activity = injected_args["activity"]
        self.initiator = injected_args["initiator"]
        self.injected_args = injected_args

    def perform_action(self):
        position = self.activity.get_position()
        resources_in_proximity = models.ResourceArea.query.filter(
            models.ResourceArea.center.ST_DWithin(
                position.wkt, models.ResourceArea.radius)) \
            .filter_by(resource_type=self.resource_type).all()

        number_of_resource_areas = len(resources_in_proximity)
        amount_of_resource = 0
        for resource in resources_in_proximity:
            amount_from_this_area = min(round(resource.efficiency / number_of_resource_areas), resource.amount)
            resource.amount -= amount_from_this_area
            amount_of_resource += amount_from_this_area

        create_item_action = CreateItemAction(item_type=self.resource_type, properties={},
                                              used_materials={}, amount=amount_of_resource, **self.injected_args)
        return create_item_action.perform()


class RemoveItemAction(ActivityAction):
    @convert(item=models.Item)
    def __init__(self, item, gracefully=True):
        self.item = item
        self.gracefully = gracefully

    def perform_action(self):
        self.item.remove(self.gracefully)


class RemoveActivityContainerAction(ActivityAction):
    def __init__(self, activity, **injected_args):
        self.activity = activity
        self.injected_args = injected_args

    def perform_action(self):
        self.activity.being_in.remove(True)


class CreateLocationAction(ActivityAction):
    @convert(location_type=models.LocationType)
    def __init__(self, *, location_type, used_materials, properties, visible_material=None, **injected_args):
        self.location_type = location_type
        self.used_materials = used_materials
        self.activity = injected_args["activity"]
        self.initiator = injected_args["initiator"]
        self.injected_args = injected_args
        self.properties = properties
        self.visible_material = visible_material if visible_material else {}

    def perform_action(self):
        result_loc = self.activity.being_in.being_in

        new_location = models.Location(result_loc, self.location_type)
        for prop_name, prop_value in self.properties.items():
            new_location.properties.append(models.EntityProperty(prop_name, prop_value))

        if self.used_materials == "all":  # all the materials used for an activity were set to build this item
            for material in models.Item.query.filter(models.Item.is_used_for(self.activity)).all():
                material.used_for = new_location
        # TODO what if used_materials is not all?

        if self.visible_material:
            set_visible_material(self.activity, self.visible_material, new_location)

        db.session.add(new_location)

        return [new_location]


@form_on_setup(entity_name=recipes.NameInput)
class AddNameToEntityAction(ActivityAction):
    def __init__(self, *, entity_name, results_index=-1, **injected_args):
        self.entity_name = entity_name
        self.entities = injected_args["resulting_entities"][results_index]

    def perform_action(self):
        for entity in self.entities:
            entity.title = self.entity_name


##############################
#      SCHEDULER ACTIONS     #
##############################


class ProcessAction(AbstractAction):
    """
    Process is a top-level class which is subclassed by all processes run by the scheduler.
    """

    def __init__(self, task):
        self.task = task


class WorkProcess(ProcessAction):
    SCHEDULER_RUNNING_INTERVAL = 10 * general.GameDate.SEC_IN_MIN

    def __init__(self, task):
        super().__init__(task)

    def perform_action(self):
        work_intents = models.Intent.query.filter_by(type=main.Intents.WORK).order_by(
            models.Intent.priority.desc()).all()

        activities_to_progress = {}
        for work_intent in work_intents:
            # in fact it shouldn't move anything, it should store intermediate data about direction and speed for each
            # RootLocation, because there can be multi-location vehicles.
            # But there can also be 2 separate veh in one RootLocation
            action_to_perform = deferred.call(work_intent.serialized_action)

            if isinstance(action_to_perform, WorkOnActivityAction):
                # activities are handled differently, because all participants must be converted at once
                if action_to_perform.activity not in activities_to_progress:
                    activities_to_progress[action_to_perform.activity] = []
                activities_to_progress[action_to_perform.activity] += [work_intent.executor]
                continue

            try:
                db.session.begin_nested()
                result = action_to_perform.perform()

                if result:  # action finished successfully and should be removed
                    logger.info("Intent %s of %s finished successfully. Removing it",
                                str(action_to_perform), str(work_intent.executor))
                    db.session.delete(work_intent)
                db.session.commit()
            except main.TurningIntoIntentExceptionMixin:
                db.session.rollback()  # it failed but this intent can be performed later
            except main.GameException as exception:
                db.session.rollback()
                self.report_failure_notification(exception.error_tag, exception.error_kwargs, work_intent.executor)
            except:  # action failed for unknown (probably not temporary) reason
                logger.error("Unknown exception prevented execution of %s", str(action_to_perform), exc_info=True)
                raise

        for activity, workers in activities_to_progress.items():
            activity_progress = ActivityProgressProcess(activity, workers)
            try:
                db.session.begin_nested()
                activity_progress.perform()
                db.session.commit()
            except main.GameException as exception:
                logger.debug("GameException prevented ActivityProgress %s ", sys.exc_info())
                db.session.rollback()  # add some user notification
                for worker in workers:
                    self.report_failure_notification(exception.error_tag, exception.error_kwargs, worker)
            except:
                logger.error("Unknown exception prevented ActivityProgress", exc_info=True)
                raise

    @classmethod
    def report_failure_notification(cls, error_tag, error_kwargs, worker):
        failure_notification = models.Notification.query.filter_by(title_tag=error_tag, title_params=error_kwargs,
                                                                    text_tag=error_tag, text_params=error_kwargs,
                                                                    character=worker, player=None).first()

        if failure_notification:
            failure_notification.count += 1
            failure_notification.update_date()
        else:
            failure_notification = models.Notification(error_tag, error_kwargs, error_tag, error_kwargs,
                                                       character=worker, player=None)
            db.session.add(failure_notification)
        main.call_hook(main.Hooks.NEW_CHARACTER_NOTIFICATION, character=worker, notification=failure_notification)


def move_entity_to_position(entity, direction, target_position):
    entity_root = entity.get_root()

    new_root_location = models.RootLocation(target_position, direction)
    db.session.add(new_root_location)
    entity.being_in = new_root_location
    main.call_hook(main.Hooks.ENTITY_CONTENTS_COUNT_DECREASED, entity=entity_root)


class FightInCombatAction(Action):
    @convert(executor=models.Entity, combat_entity=models.Combat)
    def __init__(self, executor, combat_entity, side, stance):
        super().__init__(executor)
        self.combat_entity = combat_entity
        self.side = side
        self.stance = stance

    def perform_action(self):
        foe_combat_actions = combat.get_combat_actions_of_visible_foes(self.executor, self.combat_entity)
        combat_action_of_target = combat.get_hit_target(self, foe_combat_actions)

        if combat_action_of_target:
            self.execute_hit(combat_action_of_target)

        self.perform_first_auxiliary_action()

        return combat_action_of_target, foe_combat_actions

    def execute_hit(self, targets_combat_action):
        hit_damage = self.calculate_hit_damage(targets_combat_action)

        damaged_foe = targets_combat_action.executor

        damaged_foe.damage += hit_damage
        self.combat_entity.set_recorded_damage(damaged_foe,
                                               self.combat_entity.get_recorded_damage(damaged_foe) + hit_damage)

        general.EventCreator.base(main.Events.HIT_TARGET_IN_COMBAT, rng=general.VisibilityBasedRange(10), params={},
                                  doer=self.executor,
                                  target=targets_combat_action.executor)

    def calculate_hit_damage(self, targets_combat_action):
        hit_damage = 0.1  # trololo hit damage formula
        if self.stance == CombatProcess.STANCE_OFFENSIVE:
            hit_damage *= 1.5
        if targets_combat_action.stance == CombatProcess.STANCE_DEFENSIVE:
            hit_damage /= 2
        return hit_damage

    def perform_first_auxiliary_action(self):
        auxiliary_action_to_perform = models.Intent.query.filter_by(type=main.Intents.COMBAT_AUXILIARY_ACTION,
                                                                    executor=self.executor).first()
        if auxiliary_action_to_perform:
            logger.debug("Performing auxiliary combat action: %s", auxiliary_action_to_perform)
            auxiliary_action_to_perform.perform()


class WorkOnActivityAction(Action):
    @convert(executor=models.Character, activity=models.Activity)
    def __init__(self, executor, activity):
        super().__init__(executor)
        self.activity = activity


class TravelInDirectionAction(Action):
    @convert(executor=models.Entity)
    def __init__(self, executor, direction):
        super().__init__(executor)
        self.direction = direction

    def perform_action(self):
        speed = self.executor.get_max_speed()

        initial_pos = self.executor.get_root().position

        ticks_per_day = general.GameDate.SEC_IN_DAY / WorkProcess.SCHEDULER_RUNNING_INTERVAL
        speed_per_tick = speed / ticks_per_day

        max_potential_distance = speed_per_tick * general.TraversabilityBasedRange.MAX_RANGE_MULTIPLIER

        rng = general.TraversabilityBasedRange(speed_per_tick)
        travel_distance_per_tick = rng.get_maximum_range_from_estimate(initial_pos, self.direction, speed_per_tick,
                                                                       max_potential_distance)

        destination_pos = util.pos_for_distance_in_direction(initial_pos, self.direction, travel_distance_per_tick)

        logger.info("Travel of %s from %s to %s [speed: %s]", self.executor, initial_pos, destination_pos,
                    speed_per_tick)

        move_entity_to_position(self.executor, self.direction, destination_pos)
        return False


class TravelToEntityAction(ActionOnEntity):
    """
    Responsible for moving a mobile executor to the target entity.
    """

    @convert(executor=models.Entity, entity=models.Entity)
    def __init__(self, executor, entity):
        super().__init__(executor, entity)

    def perform_action(self):
        seeing_entity_range = general.VisibilityBasedRange(20)
        if not seeing_entity_range.is_near(self.executor, self.entity):
            raise main.EntityTooFarAwayException(entity=self.entity)

        initial_pos = self.executor.get_root().position
        target_entity_root = self.entity.get_root()

        speed_per_tick = self.get_speed_per_tick()

        return self.come_closer_to_entity(initial_pos, speed_per_tick, target_entity_root)

    def get_speed_per_tick(self):
        speed = self.executor.get_max_speed()
        ticks_per_day = general.GameDate.SEC_IN_DAY / WorkProcess.SCHEDULER_RUNNING_INTERVAL
        speed_per_tick = speed / ticks_per_day
        return speed_per_tick

    def come_closer_to_entity(self, initial_position, speed_per_tick, target_entity_root):
        traversability = general.TraversabilityBasedRange(speed_per_tick,
                                                          allowed_terrain_types=[main.Types.LAND_TERRAIN])
        if traversability.is_near(self.executor, self.entity):  # move to the same root location
            move_entity_between_entities(self.executor, self.executor.being_in, target_entity_root)
            return True
        else:
            direction_to_destination = util.direction_degrees(initial_position, target_entity_root.position)
            max_potential_range = speed_per_tick * general.TraversabilityBasedRange.MAX_RANGE_MULTIPLIER
            distance_traversed = traversability.get_maximum_range_from_estimate(initial_position,
                                                                                direction_to_destination,
                                                                                speed_per_tick,
                                                                                max_potential_range)

            target_position = util.pos_for_distance_in_direction(initial_position, direction_to_destination,
                                                                 distance_traversed)

            move_entity_to_position(self.executor, direction_to_destination, target_position)
            return False


class TravelToEntityAndPerformAction(Action):
    @convert(executor=models.Character, entity=models.Entity, action=Action)
    def __init__(self, executor, entity, action):
        super().__init__(executor)
        self.entity = entity
        self.action = action

    def perform_action(self):

        travel_to_entity_action = TravelToEntityAction(self.executor, self.entity)
        travel_to_entity_action.perform()

        return self.try_to_perform_action()

    def try_to_perform_action(self):
        try:
            db.session.begin_nested()
            self.action.perform()
            db.session.commit()  # commit savepoint
            return True
        except main.TurningIntoIntentExceptionMixin:  # these exceptions result in rollback, not removal of intent
            db.session.rollback()  # rollback to savepoint
            return False


class ActivityProgressProcess(AbstractAction):
    DEFAULT_PROGRESS = 5.0

    def __init__(self, activity, workers):
        self.activity = activity
        self.workers = workers
        self.entity_worked_on = self.activity.being_in
        self.tool_based_quality = []
        self.machine_based_quality = []
        self.progress_ratio = 0.0

    def perform_action(self):
        logger.info("progress of %s", self.activity)

        req = self.activity.requirements

        activity_params = {}

        if "mandatory_machines" in req:
            logger.info("checking mandatory_machines")
            ActivityProgress.check_mandatory_machines(req["mandatory_machines"],
                                                      self.entity_worked_on.get_location(), activity_params)

        if "optional_machines" in req:
            logger.info("checking optional_machines")
            ActivityProgress.check_optional_machines(req["optional_machines"],
                                                     self.entity_worked_on.get_location(), activity_params)

        if "targets" in req:
            logger.info("checking targets")
            ActivityProgress.check_target_proximity(req["targets"], self.entity_worked_on.get_location())

        if "target_with_properties" in req:
            pass

        if "required_resources" in req:
            logger.info("checking required resources %s", req["required_resources"])
            ActivityProgress.check_required_resources(req["required_resources"], self.entity_worked_on.get_location())

        if "location_types" in req:
            logger.info("checking location type")
            ActivityProgress.check_location_types(req["location_types"], self.entity_worked_on.get_location())

        if "terrain_type" in req:
            logger.info("checking location type")
            ActivityProgress.check_terrain_types(req["terrain_type"], self.entity_worked_on.get_location())

        if "excluded_by_entities" in req:
            logger.info("checking exclusion of entities")
            ActivityProgress.check_excluded_by_entities(req["excluded_by_entities"],
                                                        self.entity_worked_on.get_location())

        if "input" in req:
            ActivityProgress.check_input_requirements(req["input"])

        if "progress_ratio" in activity_params:
            self.progress_ratio += activity_params["progress_ratio"]
        if "machine_based_quality" in activity_params:
            self.machine_based_quality = activity_params["machine_based_quality"]

        active_workers = []
        for worker in self.workers:
            try:
                worker_impact = {}
                ActivityProgress.check_worker_proximity(self.activity, worker)

                if "mandatory_tools" in req:
                    ActivityProgress.check_mandatory_tools(worker, req["mandatory_tools"], worker_impact)

                if "optional_tools" in req:
                    ActivityProgress.check_optional_tools(worker, req["optional_tools"], worker_impact)

                if "skills" in req:
                    ActivityProgress.check_skills(worker, req["skills"], worker_impact)

                if "tool_based_quality" in worker_impact:
                    self.tool_based_quality += worker_impact["tool_based_quality"]
                if "progress_ratio" in worker_impact:
                    self.progress_ratio += worker_impact["progress_ratio"]

                self.progress_ratio += ActivityProgressProcess.DEFAULT_PROGRESS
                active_workers.append(worker)
            except main.GameException as exception:
                # report the notification to worker
                WorkProcess.report_failure_notification(exception.error_tag, exception.error_kwargs, worker)

        if "max_workers" in req:
            ActivityProgress.check_min_workers(active_workers, req["min_workers"])

        if "min_workers" in req:
            ActivityProgress.check_max_workers(active_workers, req["max_workers"])

        if len(self.tool_based_quality):
            self.activity.quality_sum += mean(self.tool_based_quality)
            self.activity.quality_ticks += 1

        if len(self.machine_based_quality):
            self.activity.quality_sum += mean(self.machine_based_quality)
            self.activity.quality_ticks += 1

        mandatory_equipment_based_quality = 1
        if len(self.tool_based_quality) or len(self.machine_based_quality):
            mandatory_equipment_based_quality = mean(self.tool_based_quality + self.machine_based_quality)

        # better quality tools => faster progress
        progress_to_inflict = ActivityProgress.calculate_resultant_progress(self.progress_ratio,
                                                                            mandatory_equipment_based_quality)
        self.activity.ticks_left -= progress_to_inflict

        for group, params in req.get("input", {}).items():
            if "quality" in params:
                self.activity.quality_sum += params["quality"]
                self.activity.quality_ticks += 1

        if self.activity.ticks_left <= 0:
            ActivityProgress.finish_activity(self.activity)


class ActivityProgress:
    @classmethod
    def check_worker_proximity(cls, activity, worker):
        rng = general.SameLocationRange()

        if not worker.has_access(activity, rng=rng):
            raise main.TooFarFromActivityException(activity=activity)

    @classmethod
    def check_input_requirements(cls, materials):
        for name, material in materials.items():
            if material["left"] > 0:
                raise main.NoInputMaterialException(item_type=models.EntityType.by_name(name))

    @classmethod
    def check_mandatory_tools(cls, worker, tools, worker_impact):
        worker_impact["tool_based_quality"] = []
        for tool_type_name in tools:
            group = models.EntityType.by_name(tool_type_name)
            type_eff_pairs = group.get_descending_types()
            allowed_types = [pair[0] for pair in type_eff_pairs]

            tools = general.ItemQueryHelper.query_all_types_in(allowed_types, worker).all()
            if not tools:
                raise main.NoToolForActivityException(tool_name=group.name)

            tool_best_relative_quality = cls._get_most_efficient_item_relative_quality(tools, type_eff_pairs)

            # tool quality affects quality of activity result
            worker_impact["tool_based_quality"] += [tool_best_relative_quality]

    @classmethod
    def check_optional_tools(cls, worker, tools_progress_bonus, worker_impact):
        worker_impact["progress_ratio"] = 0.0
        for tool_type_name in tools_progress_bonus:
            group = models.EntityType.by_name(tool_type_name)
            type_eff_pairs = group.get_descending_types()
            allowed_types = [pair[0] for pair in type_eff_pairs]

            tools = general.ItemQueryHelper.query_all_types_in(allowed_types, worker).all()
            if not tools:
                continue

            tool_best_relative_quality = cls._get_most_efficient_item_relative_quality(tools, type_eff_pairs)

            # quality affects only progress ratio increased
            worker_impact["progress_ratio"] += tools_progress_bonus[tool_type_name] * tool_best_relative_quality

    @classmethod
    def _get_most_efficient_item_relative_quality(cls, tools, type_eff_pairs):
        efficiency_of_type = {pair[0]: pair[1] for pair in type_eff_pairs}
        relative_quality = lambda item: efficiency_of_type[item.type] * item.quality

        sorted_by_quality = sorted(tools, key=relative_quality, reverse=True)
        most_efficient_tool = sorted_by_quality[0]
        return relative_quality(most_efficient_tool)

    @classmethod
    def check_mandatory_machines(cls, machines, location, activity_params):
        activity_params["machine_based_quality"] = []
        for machine_name in machines:
            group = models.EntityType.by_name(machine_name)
            type_eff_pairs = group.get_descending_types()
            allowed_types = [pair[0] for pair in type_eff_pairs]

            machines = general.ItemQueryHelper.query_all_types_near(allowed_types, location).all()
            if not machines:
                raise main.NoMachineForActivityException(machine_name=group.name)

            machine_best_relative_quality = cls._get_most_efficient_item_relative_quality(machines, type_eff_pairs)

            # machine quality affects quality of activity result
            activity_params["machine_based_quality"] += [machine_best_relative_quality]

    @classmethod
    def check_optional_machines(cls, machine_progress_bonus, location, activity_params):
        for machine_type_name in machine_progress_bonus:
            group = models.EntityType.by_name(machine_type_name)
            type_eff_pairs = group.get_descending_types()
            allowed_types = [pair[0] for pair in type_eff_pairs]

            machines = general.ItemQueryHelper.query_all_types_near(allowed_types, location).all()
            if not machines:
                continue

            machine_best_relative_quality = cls._get_most_efficient_item_relative_quality(machines, type_eff_pairs)

            # quality affects only progress ratio increased
            progress_ratio_change = machine_progress_bonus[machine_type_name] * machine_best_relative_quality
            activity_params["progress_ratio"] = activity_params.get("progress_ratio", 0) + progress_ratio_change

    @classmethod
    def finish_activity(cls, activity):
        logger.info("Finishing activity %s", activity)
        entities_lists = []
        for serialized_action in activity.result_actions:
            logger.debug("executing action: %s", serialized_action)
            action = deferred.call(serialized_action, activity=activity, initiator=activity.initiator,
                                   resulting_entities=entities_lists)

            returned_entities = action.perform()

            entities_lists.append(
                returned_entities if returned_entities else [])  # always add a list (even if it's empty)

        db.session.delete(activity)

    @classmethod
    def check_target_proximity(cls, target_ids, location):
        targets = models.Entity.query.filter(models.Entity.id.in_(target_ids)).all()
        for target in targets:
            rng = general.SameLocationRange()
            if not rng.is_near(location, target):
                raise main.ActivityTargetTooFarAwayException(entity=target)

    @classmethod
    def check_min_workers(cls, active_workers, min_number):
        if len(active_workers) < min_number:
            raise main.TooFewParticipantsException(min_number=min_number)

    @classmethod
    def check_max_workers(cls, active_workers, max_number):
        if len(active_workers) > max_number:
            raise main.TooManyParticipantsException(max_number=max_number)

    @classmethod
    def check_skills(cls, worker, skills, worker_impact):
        for skill_name, min_skill_value in skills.items():

            if worker.get_skill_factor(skill_name) < min_skill_value:
                raise main.TooLowSkillException(skill_name=skill_name, required_level=min_skill_value)

    @classmethod
    def check_required_resources(cls, resources, location):
        for resource_name in resources:
            required_resource_in_area = models.ResourceArea.query.filter(
                models.ResourceArea.center.ST_DWithin(
                    location.get_position().wkt,
                    models.ResourceArea.radius)).filter_by(resource_type_name=resource_name).first()
            if not required_resource_in_area:
                raise main.NoResourceAvailableException(resource_name=resource_name)

    @classmethod
    def check_location_types(cls, location_types, location):
        if location.type.name not in location_types:
            raise main.InvalidLocationTypeException(allowed_types=location_types)

    @classmethod
    def check_terrain_types(cls, terrain_type_names, location):
        position = location.get_position()
        terrain = models.TerrainArea.query.filter(models.TerrainArea.terrain.ST_Intersects(position.wkt)) \
            .filter(models.TerrainArea.type_name.in_(terrain_type_names)).first()
        if not terrain:
            raise main.InvalidTerrainTypeException(required_types=terrain_type_names)

    @classmethod
    def check_excluded_by_entities(cls, entity_types, location):
        for entity_type_name, max_number in entity_types.items():
            number_of_entities = models.Item.query.filter_by(type_name=entity_type_name) \
                .filter(models.Entity.is_in(location)).count()
            if number_of_entities >= max_number:
                raise main.TooManyExistingEntitiesException(entity_type=entity_type_name)

    @classmethod
    def check_permanence_of_location(cls, location):
        if not location.get_root().can_be_permanent():
            raise main.TooCloseToPermanentLocation()

    @classmethod
    def calculate_resultant_progress(cls, progress_ratio, mandatory_equipment_based_quality):
        return progress_ratio * max(1, mandatory_equipment_based_quality) ** 0.5


class EatingProcess(ProcessAction):
    HUNGER_INCREASE = 0.1
    HUNGER_MAX_DECREASE = -0.2
    FOOD_BASED_ATTR_DECAY = 0.005
    FOOD_BASED_ATTR_MAX_POSSIBLE_INCREASE = 0.01

    def __init__(self, task):
        super().__init__(task)

    @staticmethod
    def bonus_mult(vals):
        return 1 + max(0, (sum(vals) / EatingProcess.FOOD_BASED_ATTR_MAX_POSSIBLE_INCREASE - 1) * 0.3)

    def perform_action(self):
        characters = models.Character.query.all()

        for character in characters:
            character.hunger += EatingProcess.HUNGER_INCREASE

            eating_queue = character.eating_queue

            hunger_attr_points = eating_queue.get("hunger")
            if hunger_attr_points:
                character.hunger += max(hunger_attr_points, EatingProcess.HUNGER_MAX_DECREASE)
                eating_queue["hunger"] -= max(hunger_attr_points, EatingProcess.HUNGER_MAX_DECREASE)

            attributes_to_increase = {}
            for attribute in properties.EdiblePropertyType.FOOD_BASED_ATTR:
                setattr(character, attribute, getattr(character, attribute) - EatingProcess.FOOD_BASED_ATTR_DECAY)

                queue_attr_points = eating_queue.get(attribute, 0)
                increase = min(queue_attr_points, EatingProcess.FOOD_BASED_ATTR_MAX_POSSIBLE_INCREASE)
                attributes_to_increase[attribute] = increase
                eating_queue[attribute] = eating_queue.get(attribute, 0) - increase

            for attribute, increase in attributes_to_increase.items():
                setattr(character, attribute, getattr(character, attribute) + increase * EatingProcess.bonus_mult(
                    attributes_to_increase.values()))
            character.eating_queue = eating_queue


class DecayProcess(ProcessAction):
    DAILY_STACKABLE_DECAY_FACTOR = 0.01
    SCHEDULER_RUNNING_INTERVAL = general.GameDate.SEC_IN_DAY

    def __init__(self, task):
        super().__init__(task)

    def perform_action(self):
        self.degrade_items()

        self.decay_progress_of_activities()

        self.decay_abandoned_activities()

    def degrade_items(self):
        items_and_props = db.session.query(models.Item, models.EntityTypeProperty).join(models.ItemType).filter(
            sql.and_(models.ItemType.name == models.EntityTypeProperty.type_name,  # ON clause
                     models.Item.role == models.Item.ROLE_BEING_IN,
                     models.EntityTypeProperty.name == P.DEGRADABLE)).all()  # handle all items
        for item, degradable_prop in items_and_props:
            item_lifetime = degradable_prop.data["lifetime"]
            damage_fraction_to_add_since_last_tick = DecayProcess.SCHEDULER_RUNNING_INTERVAL / item_lifetime
            item.damage += damage_fraction_to_add_since_last_tick

            if item.damage == 1.0:
                if item.type.stackable:
                    self.decay_stackable_item(item)
                else:
                    self.crumble_item(item)

    def decay_stackable_item(self, item):
        runs_per_day = DecayProcess.SCHEDULER_RUNNING_INTERVAL / general.GameDate.SEC_IN_DAY
        amount_left_fraction = (1 - DecayProcess.DAILY_STACKABLE_DECAY_FACTOR / runs_per_day)
        item.amount = util.round_probabilistic(item.amount * amount_left_fraction)

    def crumble_item(self, item):
        item.remove()

    def decay_progress_of_activities(self):
        # damage level for Activities is altered ONLY in WorkProcess
        activities = models.Activity.query.filter(models.Activity.ticks_left < models.Activity.ticks_needed).all()
        for activity in activities:  # decrease progress
            activity.ticks_left += min(ActivityProgressProcess.DEFAULT_PROGRESS, activity.ticks_needed)

    def decay_abandoned_activities(self):
        # activities abandoned for a long time
        activities = models.Activity.query.filter_by(damage=1.0) \
            .filter(models.Activity.ticks_left == models.Activity.ticks_needed).all()
        for activity in activities:
            items_and_props = db.session.query(models.Item, models.EntityTypeProperty).join(models.ItemType).filter(
                sql.and_(models.ItemType.name == models.EntityTypeProperty.type_name,  # ON clause
                         models.Item.is_used_for(activity),
                         models.EntityTypeProperty.name == P.DEGRADABLE)).all()  # handle all normal stackables
            for item, degradable_prop in items_and_props:
                item_lifetime = degradable_prop.data["lifetime"]
                damage_fraction_to_add_since_last_tick = DecayProcess.SCHEDULER_RUNNING_INTERVAL / item_lifetime
                item.damage += damage_fraction_to_add_since_last_tick

                if item.damage == 1.0:
                    if item.type.stackable:
                        previous_amount = item.amount
                        self.decay_stackable_item(item)
                        amount_to_be_removed = previous_amount - item.amount
                        self.update_activity_requirements(activity, amount_to_be_removed, item)
                    else:
                        self.crumble_item(item)
                        self.update_activity_requirements(activity, 1, item)

    def update_activity_requirements(self, activity, amount_to_be_removed, item):
        input_req = activity.requirements.get("input", {})
        # one item type can be used in many groups
        for group_name, requirement_params in sorted(input_req.items()):  # deterministic order
            if "used_type" in requirement_params and requirement_params["used_type"] == item.type_name:
                item_to_group_multiplier = models.EntityType.by_name(group_name).quantity_efficiency(
                    models.EntityType.by_name(requirement_params["used_type"]))
                units_used = requirement_params["needed"] - requirement_params["left"]
                units_of_group_to_be_removed = math.ceil(amount_to_be_removed * item_to_group_multiplier)
                requirement_params["left"] += min(units_of_group_to_be_removed, units_used)
                amount_which_was_just_removed = min(units_of_group_to_be_removed, units_used) / item_to_group_multiplier
                amount_to_be_removed -= min(amount_which_was_just_removed, amount_to_be_removed)

                if requirement_params["needed"] == requirement_params["left"]:
                    del requirement_params["used_type"]  # allow any type to fulfill the group

        activity.requirements = dict(activity.requirements)  # FORCE refresh


class CombatProcess(ProcessAction):
    STANCE_OFFENSIVE = "stance_offensive"
    STANCE_DEFENSIVE = "stance_defensive"
    STANCE_RETREAT = "stance_retreat"

    SIDE_ATTACKER = 0
    SIDE_DEFENDER = 1

    SCHEDULER_RUNNING_INTERVAL = 30  # 3 * general.GameDate.SEC_IN_HOUR
    INITIAL_RUN_DELAY = 3  # 6 * general.GameDate.SEC_IN_HOUR

    DAMAGE_TO_DEFEAT = 0.5

    RETREAT_CHANCE = 0.2

    @convert(combat_entity=models.Combat)
    def __init__(self, combat_entity, task):
        super().__init__(task)
        self.combat_entity = combat_entity

    def deserialized_action(self, intent):
        return deferred.call(intent.serialized_action)

    def perform_action(self):

        fighter_intents = self.combat_entity.fighters_intents()

        all_potential_targets = set()  # people who are or could have been a target of hit
        retreated_fighters_intents = set()
        for fighter_intent in fighter_intents:
            fighter_combat_action = self.deserialized_action(fighter_intent)

            target_action, potential_targets_actions = fighter_combat_action.perform()

            if target_action:
                logger.info("Fighter %s try to hit %s", fighter_intent.executor, target_action.executor)
            else:
                logger.info("Fighter %s has no target to attack", fighter_intent.executor)
            all_potential_targets = all_potential_targets.union(
                [action.executor for action in potential_targets_actions])

            if fighter_combat_action.stance == CombatProcess.STANCE_RETREAT:
                logger.debug("Try to retreat")
                if self.try_to_retreat(fighter_intent):
                    logger.debug("Retreat successful")
                    retreated_fighters_intents.add(fighter_intent)

        logger.debug("All potential targets are: %s", all_potential_targets)
        active_fighter_intents = [intent for intent in fighter_intents
                                  if intent.executor in all_potential_targets
                                  and intent not in retreated_fighters_intents]  # fighters which will stay in combat
        fighter_intents_to_remove = [intent for intent in fighter_intents if intent not in active_fighter_intents]

        for intent_to_remove in fighter_intents_to_remove:  # character not in range of any enemy
            self.withdraw_from_combat(intent_to_remove)

        fighters_able_to_fight = [intent for intent in active_fighter_intents if
                                  self.combat_entity.is_able_to_fight(intent.executor)]
        number_of_combat_sides_participating = set([self.deserialized_action(i).side for i in fighters_able_to_fight])
        there_are_fighters_on_both_sides = len(number_of_combat_sides_participating) == 2

        if not there_are_fighters_on_both_sides:
            logger.info("Not enough fighters. Removing combat")
            self.remove_combat()

    def remove_combat(self):
        db.session.delete(self.combat_entity)
        self.task.stop_repeating()

    def try_to_retreat(self, fighting_intent):
        if random.random() <= CombatProcess.RETREAT_CHANCE:
            self.withdraw_from_combat(fighting_intent)
            return True
        return False

    def withdraw_from_combat(self, intent_to_remove):
        logger.info("Fighter %s withdrew from combat", intent_to_remove.executor)
        db.session.delete(intent_to_remove)
        general.EventCreator.base(main.Events.RETREAT_FROM_COMBAT, rng=general.VisibilityBasedRange(10), params={},
                                  doer=intent_to_remove.executor)


#

##############################
#   PLAYER-SPECIFIC ACTIONS  #
##############################


#

class CreateCharacterAction(PlayerAction):
    def __init__(self, player, character_name, sex, language):
        super().__init__(player)
        self.character_name = character_name
        self.sex = sex
        self.language = language

    def perform_action(self):
        loc = models.RootLocation.query.order_by(func.random()).first()
        new_char = models.Character(self.character_name, self.sex, self.player, self.language,
                                    general.GameDate.now(), loc.position, loc)
        db.session.add(new_char)

        return new_char


#


##############################
# CHARACTER-SPECIFIC ACTIONS #
##############################


def move_entity_between_entities(entity, source, destination, amount=1, to_be_used_for=False):
    if entity.parent_entity == source:

        assert isinstance(entity, models.Entity) and not isinstance(entity,
                                                                    models.Location), "moving locations not supported"

        if isinstance(entity, models.Item) and entity.type.stackable:
            weight = amount * entity.type.unit_weight
            move_stackable_resource(entity, source, destination, weight, to_be_used_for)
        elif to_be_used_for:
            entity.used_for = destination
        else:
            entity.being_in = destination
        main.call_hook(main.Hooks.ENTITY_CONTENTS_COUNT_DECREASED, entity=source)
    else:
        raise main.InvalidInitialLocationException(entity=entity)


def move_stackable_resource(item, source, goal, weight, to_be_used_for=False):
    # remove from the source
    if item.weight == weight:
        item.remove()
    else:
        item.weight -= weight

    # add to the goal
    if to_be_used_for:
        existing_pile = models.Item.query.filter_by(type=item.type) \
            .filter(models.Item.is_used_for(goal)).filter_by(visible_parts=item.visible_parts).first()
    else:
        existing_pile = models.Item.query.filter_by(type=item.type). \
            filter(models.Item.is_in(goal)).filter_by(visible_parts=item.visible_parts).first()

    if existing_pile:
        existing_pile.weight += weight
    else:
        new_pile = models.Item(item.type, goal, weight=weight, role_being_in=not to_be_used_for)
        new_pile.visible_parts = item.visible_parts
        db.session.add(new_pile)


def overwrite_item_amount(item, amount):
    if item.type.stackable:
        return dict(item_amount=amount)
    return {}


class DropItemAction(ActionOnItem):
    def __init__(self, executor, item, amount=1):
        super().__init__(executor, item)
        self.amount = amount

    def perform_action(self):
        if not self.executor.has_access(self.item, rng=general.InsideRange()):
            raise main.EntityNotInInventoryException(entity=self.item)

        if self.amount > self.item.amount:
            raise main.InvalidAmountException(amount=self.amount)

        move_entity_between_entities(self.item, self.executor, self.executor.being_in, self.amount)

        event_args = self.item.pyslatize(**overwrite_item_amount(self.item, self.amount))

        general.EventCreator.base(Events.DROP_ITEM, self.rng, event_args, self.executor)


class TakeItemAction(ActionOnItem):
    def __init__(self, executor, item, amount=1):
        super().__init__(executor, item)
        self.amount = amount

    def perform_action(self):
        if not self.executor.has_access(self.item, rng=general.SameLocationRange()):
            raise main.EntityTooFarAwayException(entity=self.item)

        if self.amount < 0 or self.amount > self.item.amount:
            raise main.InvalidAmountException(amount=self.amount)

        move_entity_between_entities(self.item, self.executor.being_in, self.executor, self.amount)

        event_args = self.item.pyslatize(**overwrite_item_amount(self.item, self.amount))
        general.EventCreator.base(Events.TAKE_ITEM, self.rng, event_args, self.executor)


class GiveItemAction(ActionOnItemAndCharacter):
    def __init__(self, executor, item, receiver, amount=1):
        super().__init__(executor, item, receiver)
        self.amount = amount

    def perform_action(self):
        if not self.executor.has_access(self.item, rng=general.InsideRange()):
            raise main.EntityNotInInventoryException(entity=self.item)

        if self.amount > self.item.amount:
            raise main.InvalidAmountException(amount=self.amount)

        if not self.character:  # has not enough space in inventory
            raise main.OwnInventoryExceededException()

        move_entity_between_entities(self.item, self.executor, self.character, self.amount)

        event_args = self.item.pyslatize(**overwrite_item_amount(self.item, self.amount))
        general.EventCreator.base(Events.GIVE_ITEM, self.rng, event_args,
                                  self.executor, self.character)


class AddEntityToActivityAction(ActionOnItemAndActivity):
    def __init__(self, executor, item, activity, amount):
        super().__init__(executor, item, activity)
        self.amount = amount

    def perform_action(self):

        if not self.executor.has_access(self.item, rng=general.SameLocationRange()):
            raise main.EntityNotInInventoryException(entity=self.item)

        if not self.executor.has_access(self.activity, rng=general.SameLocationRange()):
            raise main.TooFarFromActivityException(activity=self.activity)

        if self.amount > self.item.amount:
            raise main.InvalidAmountException(amount=self.amount)

        req = self.activity.requirements

        for required_group_name, required_group_params in sorted(req.get("input", {}).items()):  # TODO make it prettier
            required_group = models.EntityType.by_name(required_group_name)
            if "used_type" in required_group_params:
                if required_group_params["used_type"] != self.item.type_name:  # must be exactly the same type
                    raise main.OnlySpecificTypeForGroupException(
                        type_name=required_group_params["used_type"], group_name=required_group_name)
            if required_group_params["left"] == 0:  # this requirement is fulfilled
                continue
            if not required_group.contains(self.item.type):  # requirement cannot be fulfilled by this type
                continue
            type_efficiency_ratio = required_group.quantity_efficiency(self.item.type)
            max_to_be_added = math.ceil(required_group_params["left"] / type_efficiency_ratio)
            amount_to_add = min(self.amount, max_to_be_added)

            source = self.item.being_in  # on ground
            if self.item.being_in == self.executor:  # in inventory
                source = self.executor

            move_entity_between_entities(self.item, source, self.activity, amount_to_add, to_be_used_for=True)

            material_left_reduction = amount_to_add * type_efficiency_ratio

            required_group_params["left"] = max(0, required_group_params["left"] - material_left_reduction)
            required_group_params["used_type"] = self.item.type_name
            if not self.item.type.stackable:  # non-stackables affect quality
                added_to_needed_fraction = material_left_reduction / required_group_params["needed"]
                total_item_quality = required_group.quality_efficiency(self.item.type) * self.item.quality
                required_group_params["quality"] = (total_item_quality * added_to_needed_fraction) \
                                                   + required_group_params.get("quality", 0)

            overwrites = {}
            if self.item.type.stackable:
                overwrites["item_amount"] = amount_to_add

            item_info = self.item.pyslatize(**overwrite_item_amount(self.item, amount_to_add))
            event_args = {"groups": {
                "item": item_info,
                "activity": self.activity.pyslatize()
            }}
            general.EventCreator.base(Events.ADD_TO_ACTIVITY, self.rng, event_args, doer=self.executor)
            break
        else:
            raise main.ItemNotApplicableForActivityException(item=self.item, activity=self.activity)

        self.activity.requirements = {}  # TODO CATASTROPHE
        db.session.flush()
        self.activity.requirements = req


class EatAction(ActionOnItem):
    @convert(executor=models.Character, item=models.Item)
    def __init__(self, executor, item, amount):
        super().__init__(executor, item, rng=general.VisibilityBasedRange(20))
        self.amount = amount

    def perform_action(self):

        if not self.executor.has_access(self.item, rng=general.TraversabilityBasedRange(10, allowed_terrain_types=[
            main.Types.LAND_TERRAIN])):
            raise main.EntityTooFarAwayException(entity=self.item)

        if self.item.amount < self.amount:
            raise main.InvalidAmountException(amount=self.amount)

        if self.item.get_max_edible(self.executor) < self.amount:
            raise main.InvalidAmountException(amount=self.amount)

        self.item.amount -= self.amount

        self.item.eat(self.executor, self.amount)

        food_item_info = self.item.pyslatize(item_amount=self.amount, detailed=False)
        general.EventCreator.base(Events.EAT, self.rng, {"groups": {"food": food_item_info}}, doer=self.executor)

        main.call_hook(main.Hooks.EATEN, character=self.executor, item=self.item, amount=self.amount)


class SayAloudAction(ActionOnSelf):
    def __init__(self, executor, message):
        super().__init__(executor, rng=general.VisibilityBasedRange(20))
        self.message = message

    def perform_action(self):
        general.EventCreator.base(Events.SAY_ALOUD, self.rng, {"message": self.message}, doer=self.executor)

        main.call_hook(main.Hooks.SPOKEN_ALOUD, character=self.executor)


class SpeakToSomebodyAction(ActionOnCharacter):
    def __init__(self, executor, character, message):
        super().__init__(executor, character, rng=general.VisibilityBasedRange(20))
        self.message = message

    def perform_action(self):
        if not self.executor.has_access(self.character, rng=general.VisibilityBasedRange(20)):
            raise main.EntityTooFarAwayException(entity=self.character)

        general.EventCreator.base(Events.SPEAK_TO_SOMEBODY, self.rng, {"message": self.message}, doer=self.executor,
                                  target=self.character)


class WhisperToSomebodyAction(ActionOnCharacter):
    def __init__(self, executor, character, message):
        super().__init__(executor, character, rng=general.VisibilityBasedRange(20))
        self.message = message

    def perform_action(self):
        if not self.executor.has_access(self.character, rng=general.SameLocationRange()):
            raise main.EntityTooFarAwayException(entity=self.character)

        general.EventCreator.base(Events.WHISPER, self.rng, {"message": self.message}, doer=self.executor,
                                  target=self.character)

        main.call_hook(main.Hooks.WHISPERED, character=self.executor, to_character=self.character)


class JoinActivityAction(ActionOnActivity):
    def __init__(self, executor, activity):
        super().__init__(executor, activity, rng=None)

    def perform_action(self):
        if not self.executor.has_access(self.activity, rng=general.SameLocationRange()):
            raise main.TooFarFromActivityException(activity=self.activity)

        # only 1 activity allowed at once TODO #72
        models.Intent.query.filter_by(executor=self.executor, type=main.Intents.WORK).delete()

        work_intent = models.Intent(self.executor, main.Intents.WORK, 1, self.activity,
                                    deferred.serialize(WorkOnActivityAction(self.executor, self.activity)))
        db.session.add(work_intent)


class MoveToLocationAction(ActionOnLocation):
    def __init__(self, executor, passage):
        if executor.being_in == passage.left_location:
            location = passage.right_location
        else:
            location = passage.left_location
        super().__init__(executor, location, rng=general.SameLocationRange())
        self.passage = passage

    def perform_action(self):

        # TODO check if passage is locked

        if not self.executor.has_access(self.passage, rng=general.SameLocationRange()):
            raise main.EntityTooFarAwayException(entity=self.location)

        from_loc = self.executor.being_in
        if not self.passage.between(from_loc, self.location):
            raise main.EntityTooFarAwayException(entity=self.location)  # TODO Better event?

        general.EventCreator.base(Events.MOVE, self.rng, {"groups": {"from": from_loc.pyslatize(),
                                                                     "destination": self.location.pyslatize()}},
                                  doer=self.executor)

        self.executor.being_in = self.location

        general.EventCreator.create(rng=general.SameLocationRange(), tag_observer=Events.MOVE + "_observer",
                                    params={"groups": {"from": from_loc.pyslatize(),
                                                       "destination": self.location.pyslatize()}},
                                    doer=self.executor)

        main.call_hook(main.Hooks.LOCATION_ENTERED, character=self.executor, from_loc=from_loc, to_loc=self.location)


class AttackCharacterAction(ActionOnCharacter):
    def __init__(self, executor, target_character):
        super().__init__(executor, target_character, rng=general.VisibilityBasedRange(10))

    def perform_action(self):
        if self.executor == self.character:
            raise main.CannotAttackYourselfException()
        if not self.executor.has_access(self.character,
                                        rng=general.VisibilityBasedRange(30)):  # TODO THE SAME RANGE AS COMBAT
            raise main.EntityTooFarAwayException(entity=self.character)

        if models.Intent.query.filter_by(executor=self.executor, type=main.Intents.COMBAT).count():
            raise main.AlreadyBeingInCombat()
        if models.Intent.query.filter_by(executor=self.character, type=main.Intents.COMBAT).count():
            raise main.TargetAlreadyInCombat(character=self.character)

        combat_entity = models.Combat()
        db.session.add(combat_entity)
        db.session.flush()

        fighting_action = FightInCombatAction(self.executor, combat_entity,
                                              CombatProcess.SIDE_ATTACKER, CombatProcess.STANCE_OFFENSIVE)
        combat_intent = models.Intent(self.executor, main.Intents.COMBAT, 1, combat_entity,
                                      deferred.serialize(fighting_action))
        foe_fighting_action = FightInCombatAction(self.character, combat_entity,
                                                  CombatProcess.SIDE_DEFENDER, CombatProcess.STANCE_OFFENSIVE)
        foe_combat_intent = models.Intent(self.character, main.Intents.COMBAT, 1, combat_entity,
                                          deferred.serialize(foe_fighting_action))

        # create combat process
        current_timestamp = general.GameDate.now().game_timestamp
        execution_timestamp = general.GameDate(current_timestamp + CombatProcess.INITIAL_RUN_DELAY).game_timestamp
        combat_process = deferred.serialize(CombatProcess(combat_entity, None))
        task = models.ScheduledTask(combat_process, execution_timestamp, CombatProcess.SCHEDULER_RUNNING_INTERVAL)
        db.session.add_all([combat_intent, foe_combat_intent, task])

        general.EventCreator.base(main.Events.ATTACK_CHARACTER, self.rng, doer=self.executor, target=self.character)


class JoinCombatAction(ActionOnEntity):
    def __init__(self, executor, combat_entity, side):
        super().__init__(executor, combat_entity, rng=general.VisibilityBasedRange(10))
        self.side = side

    def perform_action(self):
        if self.side not in [CombatProcess.SIDE_ATTACKER, CombatProcess.SIDE_DEFENDER]:
            raise ValueError("{} is an invalid side in combat".format(self.side))

        fighting_action = FightInCombatAction(self.executor, self.entity,
                                              self.side, CombatProcess.STANCE_OFFENSIVE)

        combat_intent = models.Intent(self.executor, main.Intents.COMBAT, 1, self.entity,
                                      deferred.serialize(fighting_action))
        db.session.add(combat_intent)

        general.EventCreator.base(main.Events.JOIN_COMBAT, self.rng, doer=self.executor)


class ChangeCombatStanceAction(ActionOnSelf):
    def __init__(self, executor, new_stance):
        super().__init__(executor)
        self.new_stance = new_stance

    def perform_action(self):
        if self.new_stance not in [CombatProcess.STANCE_OFFENSIVE,
                                   CombatProcess.STANCE_DEFENSIVE,
                                   CombatProcess.STANCE_RETREAT]:
            raise ValueError("{} is an invalid stance".format(self.new_stance))

        combat_intent = models.Intent.query.filter_by(executor=self.executor, type=main.Intents.COMBAT).one()

        own_combat_action = deferred.call(combat_intent.serialized_action)
        own_combat_action.stance = self.new_stance
        combat_intent.serialized_action = deferred.serialize(own_combat_action)


class ToggleCloseableAction(ActionOnEntity):
    def __init__(self, executor, closeable_entity):
        super().__init__(executor, closeable_entity)

    def perform_action(self):

        # TODO check if entity is locked

        if not self.executor.has_access(self.entity, rng=general.SameLocationRange()):
            raise main.EntityTooFarAwayException(entity=self.entity)

        closeable_prop = self.entity.get_property(P.CLOSEABLE)
        going_to_open = closeable_prop["closed"]
        self.entity.alter_property(P.CLOSEABLE, {"closed": not going_to_open})

        if going_to_open:
            event_name = Events.OPEN_ENTITY
        else:
            event_name = Events.CLOSE_ENTITY

        general.EventCreator.base(event_name, self.rng, {"groups": {"entity": self.entity.pyslatize()}},
                                  doer=self.executor)


class DeathAction(Action):
    def __init__(self, executor):
        super().__init__(executor)

    def turn_into_body(self):
        self.executor.type = models.EntityType.by_name(main.Types.DEAD_CHARACTER)

    @staticmethod
    def create_death_info_property():
        return models.EntityProperty(P.DEATH_INFO,
                                     {"date": general.GameDate.now().game_timestamp})


class DeathOfStarvationAction(DeathAction):
    def __init__(self, executor):
        super().__init__(executor)

    def perform_action(self):
        general.EventCreator.base(main.Events.DEATH_OF_STARVATION, rng=general.VisibilityBasedRange(30),
                                  doer=self.executor)

        death_prop = self.create_death_info_property()
        death_prop.data["cause"] = models.Character.DEATH_STARVATION
        self.executor.properties.append(death_prop)
        self.turn_into_body()


class DeathOfDamageAction(DeathAction):
    def __init__(self, executor, killer, weapon):  # apparently, executor is the executed character ;)
        super().__init__(executor)
        self.killer = killer
        self.weapon = weapon

    def perform_action(self):
        general.EventCreator.base(main.Events.DEATH_OF_DAMAGE, rng=general.VisibilityBasedRange(30), doer=self.executor,
                                  params=dict(killer=self.killer, weapon=self.weapon))

        death_prop = self.create_death_info_property()
        death_prop.data["cause"] = models.Character.DEATH_WEAPON
        death_prop.data["weapon"] = self.weapon.type.name
        death_prop.data["killer_id"] = self.killer.id
        self.executor.properties.append(death_prop)
        self.turn_into_body()
