from src.model import Influence, get_updates, get_influences, get_transitions, get_path_to_attribute
import src.simulator.sourcehelper as SH
import ast
from .to_z3 import Z3Converter, get_z3_var, get_z3_value, get_z3_variable, get_minimum_dt_of_several, to_python
from .z3conditionchangecalculator import Z3ConditionChangeCalculator
from .z3calculator import Z3Calculator
import z3

import logging
import pprint
logger = logging.getLogger(__name__)


class ConditionTimedChangeCalculator(Z3Calculator):

    def get_next_behaviour_change_time(self, entity=None):
        """calculates the time until one of the  behaviours changes (transition fire or if/else change in update/influence)"""
        if not entity:
            entity = self.entity
        logger.debug(f"Calculating next behaviour change time for entity {entity._name} ({entity.__class__.__name__}) and it's subentities")
        system_times = self.calculate_system(entity)

        # system_times = [t for e in get_all_entities(self.entity) for t in self.collect_transition_times_from_entity(e)]
        logger.debug("All behaviour changes in entity %s (%s): ", entity._name, entity.__class__.__name__)
        # logger.debug(str([(e._name, f"{t.source._name} -> {t.target._name} ({name})", dt) for (e, t, name, dt) in system_times]))

        if len(system_times) > 0:
            min_dt = get_minimum_dt_of_several(system_times, self.timeunit, self.epsilon)
            # minimum = min(system_times, key=lambda t: t[3])
            logger.debug(f"Next behaviour change in: {min_dt}")
            return min_dt
        else:
            # this happens if there are no transitions fireable by increasing time only
            return None

    def calculate_entity_hook(self, entity):
        all_dts = []
        logger.debug(f"Calculating behaviour change for entity {entity._name} ({entity.__class__.__name__})")
        for influence in get_influences(entity):
            if self.contains_if_condition(influence):
                inf_dts = self.get_condition_change_enablers(influence)
                all_dts.append(inf_dts)

        # updates = [up for up in get_updates(self.entity) if up.state == up._parent.current]
        for update in get_updates(entity):
            if update.state == update._parent.current:  # only the currently active updates
                if self.contains_if_condition(update):
                    up_dts = self.get_condition_change_enablers(update)
                    all_dts.append(up_dts)

        # TODO: check for transitions whether they can be done by time only
        for name, trans in get_transitions(entity, as_dict=True).items():
            if entity.current == trans.source:
                trans_dts = self.get_transition_time(trans)
                all_dts.append(trans_dts)

        if logger.getEffectiveLevel() <= logging.DEBUG:
            # XXX the code below makes CREST slow
            # We should only run it in debug mode, then we might as well return the smallest time
            min_dt = get_minimum_dt_of_several(all_dts, self.timeunit, self.epsilon)
            if min_dt is not None:
                logger.debug(f"Minimum behaviour change time for entity {entity._name} ({entity.__class__.__name__}) is {min_dt}")
            return [min_dt]  # return a list
        else:
            # XXX This is the faster one that we run when we're not debugging !!
            return all_dts

    def contains_if_condition(self, influence_update):
        if hasattr(influence_update, "_cached_contains_if"):
            return influence_update._cached_contains_if

        if not hasattr(influence_update, "_cached_ast"):
            influence_update._cached_ast = SH.get_ast_body(influence_update.function)

        influence_update._cached_contains_if = SH.ast_contains_node(influence_update._cached_ast, ast.If)
        return influence_update._cached_contains_if

    def get_condition_change_enablers(self, influence_update):
        logger.debug(f"Calculating condition change time in '{influence_update._name}' in entity '{influence_update._parent._name}' ({influence_update._parent.__class__.__name__})")
        solver = z3.Optimize()

        # build a mapping that shows the propagation of information to the influence/update source (what influences the guard)
        if isinstance(influence_update, Influence):
            modifier_map = self.get_modifier_map([influence_update.source, influence_update.target])
        else:
            read_ports = SH.get_accessed_ports(influence_update.function, influence_update)
            read_ports.append(influence_update.target)
            modifier_map = self.get_modifier_map(read_ports)

        z3var_constraints, z3_vars = self.get_z3_vars(modifier_map)
        solver.add(z3var_constraints)
        # NOTE: we do not add a "dt >= 0" or "dt == 0" constraint here, because it would break the solving

        # create the constraints for updates and influences
        for port, modifiers in modifier_map.items():
            for modifier in modifiers:
                if modifier != influence_update:  # skip the one we're actually analysing, this should be already done in the modifier-map creation...
                    constraints = self._get_constraints_from_modifier(modifier, z3_vars)
                    solver.add(constraints)

        solver.push()  # backup

        # if it's an influence, we need to add the source param equation
        if isinstance(influence_update, Influence):
            z3_src = z3_vars[influence_update.source][influence_update.source._name]
            params = SH.get_param_names(influence_update.function)
            param_key = params[0] + "_" + str(id(influence_update))
            z3_param = get_z3_variable(influence_update.source, params[0], str(id(influence_update)))

            if logger.getEffectiveLevel() <= logging.DEBUG:  # only log if the level is appropriate, since z3's string-conversion takes ages
                logger.debug(f"adding param: z3_vars[{param_key}] = {params[0]}_0 : {z3_param} ")

            z3_vars[param_key] = {params[0] + "_0": z3_param}

            if logger.getEffectiveLevel() <= logging.DEBUG:  # only log if the level is appropriate, since z3's string-conversion takes ages
                logger.debug(f"influence entry constraint: {z3_src} == {z3_param}")
            solver.add(z3_src == z3_param)

        solver.push()

        conv = Z3ConditionChangeCalculator(z3_vars, entity=influence_update._parent, container=influence_update, use_integer_and_real=self.use_integer_and_real)
        conv.set_solver(solver)
        min_dt = conv.get_min_behaviour_change_dt(influence_update.function)
        if min_dt is not None:
            logger.info(f"Minimum condition change times in '{influence_update._name}' in entity '{influence_update._parent._name}' ({influence_update._parent.__class__.__name__}) is {min_dt}")
        return (min_dt, influence_update)

    def get_transition_time(self, transition):
        """
        - we need to find a solution for the guard condition (e.g. using a theorem prover)
        - guards are boolean expressions over port values
        - ports are influenced by Influences starting at other ports (find recursively)
        """
        logger.debug(f"Calculating the transition time of '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__})")
        solver = z3.Optimize()

        # find the ports that influence the transition
        transition_ports = SH.get_accessed_ports(transition.guard, transition)
        # logger.debug(f"The transitions influencing ports are called: {[p._name for p in transition_ports]}")
        # build a mapping that shows the propagation of information to the guard (what influences the guard)
        modifier_map = self.get_modifier_map(transition_ports)

        z3var_constraints, z3_vars = self.get_z3_vars(modifier_map)
        solver.add(z3var_constraints)
        solver.add(z3_vars['dt'] >= 0)

        # create the constraints for updates and influences
        for port, modifiers in modifier_map.items():
            for modifier in modifiers:
                constraints = self._get_constraints_from_modifier(modifier, z3_vars)
                solver.add(constraints)

        # for port, modifiers in modifier_map.items():
            # write pre value

        # logger.debug(f"adding constraints for transition guard: {transition._name}")
        conv = Z3Converter(z3_vars, entity=transition._parent, container=transition, use_integer_and_real=self.use_integer_and_real)
        solver.add(conv.to_z3(transition.guard))

        # import pprint;pprint.pprint(z3_vars)

        # if logger.getEffectiveLevel() <= logging.DEBUG:  # only log if the level is appropriate, since z3's string-conversion takes ages
        #     logger.debug(f"Constraints handed to solver:\n{solver}")

        objective = solver.minimize(z3_vars['dt'])  # find minimal value of dt
        check = solver.check()
        # logger.debug("satisfiability: %s", check)
        if solver.check() == z3.sat:
            if logger.getEffectiveLevel() <= logging.INFO:
                logger.info(f"Minimum time to enable transition '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__}) will be enabled in {to_python(objective.value())}")
            return (objective.value(), transition)
        elif check == z3.unknown:
            if logger.getEffectiveLevel() <= logging.WARNING:
                logger.warning(f"The calculation of the minimum transition time for '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__}) was UNKNOWN. This usually happening in the presence of non-linear constraints. Do we have any?")
            std_solver = z3.Solver()
            std_solver.add(solver.assertions())
            std_solver_check = std_solver.check()
            if std_solver_check == z3.sat:
                min_dt = std_solver.model()[z3_vars['dt']]
                if logger.getEffectiveLevel() <= logging.INFO:
                    logger.info(f"We did get a solution using the standard solver though: {to_python(min_dt)} Assuming that this is the smallest solution. CAREFUL THIS MIGHT BE WRONG (especially when the transition is an inequality constraint)!!!")
                return (min_dt, transition)
            elif std_solver_check == z3.unknown:
                logger.error(f"The standard solver was also not able to decide if there is a solution or not. The constraints are too hard!!!")
                return None
            else:
                logger.info("The standard solver says there is no solution to the constraints. This means we also couldn't minimize. Problem solved.")
                return None
        else:
            logger.debug(f"Constraint set to enable transition '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__}) is unsatisfiable.")
            return None
