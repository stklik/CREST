from crestdsl import model
from crestdsl.simulation import sourcehelper as SH
import ast
from .to_z3 import Z3Converter, get_z3_variable, get_z3_var, get_z3_value, get_minimum_dt_of_several
from .z3conditionchangecalculator import Z3ConditionChangeCalculator, get_behaviour_change_dt_from_constraintset
from .z3calculator import Z3Calculator
from .conditiontimedchangecalculator import ConditionTimedChangeCalculator

import z3
from crestdsl.config import to_python
from .epsilon import Epsilon, eps_string

from types import SimpleNamespace

import threading

import logging
logger = logging.getLogger(__name__)


# TODO: extract this function and put in a central place
# also do this in other files
def log_if_level(level, message):
    """Logs the message if the level is below the specified level"""
    if logger.getEffectiveLevel() <= level:
        logger.log(level, message)

def translate_to_context(cache, ctx):
    translated = SimpleNamespace()
    # set context
    translated.ctx = ctx
    
    #translate variables
    translated.z3_vars = {}
    for port, value in cache.z3_vars.items():
        if port == "dt":
            translated.z3_vars[port] = value.translate(ctx)
        elif isinstance(port, model.Port):
            portname = port._name
            portname_with_parent = port._parent._name + "." + port._name
            
            variable = value[port._name].translate(ctx)
            pre_var = value[port._name + "_0"].translate(ctx)
            
            z3_vars[port] = {
                portname: variable,
                portname_with_parent: variable,
                portname + "_0": pre_var,
                portname_with_parent + "_0": pre_var,
                portname + ".pre": pre_var,
                portname_with_parent + ".pre": pre_var,
            }
        else:
            logger.error(f"Don't know what to do with {port} {type(port)}. Value = \n {value}")
            raise ValueError("cannot translate port to context")
    
    # translated modifier constraints
    translated.z3_modifier_constraints = {}
    for modifier, constraints in cache.z3_modifier_constraints.items():
        if isinstance(modifier, model.Transition):
            if isinstance(modifier, bool):
                guard_const = z3.And(constraints, ctx)
            else:
                guard_const = [gc.translate(ctx) for gc in constraints]
            translated.z3_modifier_constraints[modifier] = guard_const
        else:
            translated.z3_modifier_constraints[modifier] = [const.translate(ctx) for const in constraints]
    
    # translate conditionchagned sets
    translated.z3_conditionchanged_constraintsets = {}
    for modifier, (constraintset, additionals) in cache.z3_conditionchanged_constraintsets.items():
        new_entry = (constraintset.translate(ctx), [a.translate(ctx) for a in additionals])
        translated.z3_conditionchanged_constraintsets[modifier] = new_entry
        
    return translated

class FastConditionTimedChangeCalculator(ConditionTimedChangeCalculator):

    def init_z3_constraints_and_vars(self):
        logger.debug("Initializing z3 constraints and variables")
        entity = self.entity

        # load from a cache, if it exists
        if hasattr(entity, "_constraint_cache"):
            logger.debug("Initializing from cache")
            self.cache = entity._constraint_cache
            return
        
        self.cache = SimpleNamespace()
        
        # create port variables for all ports
        self.cache.z3_vars = {}
        self.cache.z3_port_constraints = {}
        
        dt_var = get_z3_var(self.timeunit, 'dt')
        self.cache.z3_vars['dt'] = dt_var
        self.cache.z3_vars['dt'].type = self.timeunit
        
        for port in model.get_all_ports(entity):
            portname = port._name
            portname_with_parent = port._parent._name + "." + port._name
            
            variable = get_z3_variable(port, port._name)
            pre_var = get_z3_variable(port, port._name + "_0")
            
            self.cache.z3_vars[port] = {
                portname: variable,
                portname_with_parent: variable,
                portname + "_0": pre_var,
                portname_with_parent + "_0": pre_var,
                portname + ".pre": pre_var,
                portname_with_parent + ".pre": pre_var,
            }
            
            pre_value = get_z3_value(port, port._name + "_0")
            self.cache.z3_port_constraints[port] = pre_var == pre_value  # init condition needs to be set
            
        # create entity constraints for all modifiers
        self.cache.z3_modifier_constraints = {}
        self.cache.z3_conditionchanged_constraintsets = {}
        for influence in model.get_all_influences(entity):
            constraints = self._get_constraints_from_modifier(influence, self.cache.z3_vars, cache=False)
            self.cache.z3_modifier_constraints[influence] = constraints
            
            # TODO: this should be nicer somehow ...
            # add port and constraint for the influence param
            z3_src = self.cache.z3_vars[influence_update.source][influence_update.source._name]
            params = SH.get_param_names(influence_update.function)
            param_key = params[0] + "_" + str(id(influence_update))
            z3_param = get_z3_variable(influence_update.source, params[0], str(id(influence_update)))
            z3_vars[param_key] = {params[0] + "_0": z3_param}
            
            conv = Z3ConditionChangeCalculator(self.cache.z3_vars, entity=influence._parent, container=influence, use_integer_and_real=self.use_integer_and_real)
            self.cache.z3_conditionchanged_constraintsets[influence] = (conv.calculate_constraints(influence.function), z3_src == z3_param )
            
        for update in model.get_all_updates(entity):
            constraints = self._get_constraints_from_modifier(update, self.cache.z3_vars, cache=False)
            self.cache.z3_modifier_constraints[update] = constraints
            
            conv = Z3ConditionChangeCalculator(self.cache.z3_vars, entity=update._parent, container=update, use_integer_and_real=self.use_integer_and_real)
            self.cache.z3_conditionchanged_constraintsets[update] = (conv.calculate_constraints(update.function), [])
            
        for transition in model.get_all_transitions(entity):
            conv = Z3Converter(self.cache.z3_vars, entity=transition._parent, container=transition, use_integer_and_real=self.use_integer_and_real)
            guard_constraint = conv.to_z3(transition.guard)
            self.cache.z3_modifier_constraints[transition] = guard_constraint

        entity._constraint_cache = self.cache

    def calculate_system(self, entity=None, include_subentities=True):
        logger.debug("FAST: Calculating for all entities")
        if not hasattr(self, "cache"):
            self.init_z3_constraints_and_vars()
        
        all_dts = []
        workers = []
        
        logger.debug(f"PARALLEL: Calculating behaviour change for entity {entity._name} ({entity.__class__.__name__})")
        
        for influence in model.get_all_influences(entity):
            if self.contains_if_condition(influence):
                ctx_i = z3.Context()
                worker = threading.Thread(target=self.get_condition_change_enablers, args=(influence, all_dts, ctx_i, self.cache))
                worker.start()
                workers.append(worker)
                worker.join()
        
        
        # updates = [up for up in get_updates(self.entity) if up.state == up._parent.current]
        for update in model.get_all_updates(entity):
            if update.state is update._parent.current:  # only the currently active updates
                if self.contains_if_condition(update):
                    ctx_i = z3.Context()
                    worker = threading.Thread(target=self.get_condition_change_enablers, args=(update, all_dts, ctx_i, self.cache))
                    worker.start()
                    workers.append(worker)
                    worker.join()

        # TODO: check for transitions whether they can be done by time only
        for trans in model.get_all_transitions(entity):
            if trans._parent.current is trans.source:
                ctx_i = z3.Context()
                worker = threading.Thread(target=self.get_transition_time, args=(trans, all_dts, ctx_i, self.cache))
                worker.start()
                workers.append(worker)
                worker.join()

        # print(f"Working on {len(workers)} threads")
        # for worker in workers:
        #     worker.join()

        return all_dts

    def get_condition_change_enablers(self, influence_update, all_dts, ctx, cache):
        """ Calculates if an if/else condition within the function can change its value """
        logger.debug(f"Calculating condition change time in '{influence_update._name}' in entity '{influence_update._parent._name}' ({influence_update._parent.__class__.__name__})")
        solver = z3.Optimize(ctx)
    
        # build a mapping that shows the propagation of information to the influence/update source (what influences the guard)
        if isinstance(influence_update, model.Influence):
            modifier_map = self.get_modifier_map([influence_update.source, influence_update.target])
        else:
            read_ports = SH.get_accessed_ports(influence_update.function, influence_update)
            read_ports.append(influence_update.target)
            modifier_map = self.get_modifier_map(read_ports)
    
        z3_vars = {}
        for port, value in cache.z3_vars.items():
            if port == "dt":
                z3_vars[port] = value.translate(ctx)
            elif isinstance(port, model.Port):
                portname = port._name
                portname_with_parent = port._parent._name + "." + port._name
                
                variable = value[port._name].translate(ctx)
                pre_var = value[port._name + "_0"].translate(ctx)
                
                z3_vars[port] = {
                    portname: variable,
                    portname_with_parent: variable,
                    portname + "_0": pre_var,
                    portname_with_parent + "_0": pre_var,
                    portname + ".pre": pre_var,
                    portname_with_parent + ".pre": pre_var,
                }
            else:
                logger.error(f"Don't know what to do with {port} {type(port)}. Value = \n {value}")
                raise ValueError("cannot translate port to context")
    
    
        # add the initial values for the sources of the dataflow
        for port, modifiers in modifier_map.items():
            # set default port value to the current value
            pre_value = get_z3_value(port, port._name + "_0").translate(ctx)
            solver.add(z3_vars[port][port._name + "_0"] == pre_value)
            if len(modifiers) == 0:
                solver.add(z3_vars[port][port._name] == z3_vars[port][port._name + "_0"])
    
        # create the constraints for updates and influences
        for port, modifiers in modifier_map.items():
            for modifier in modifiers:
                if modifier != influence_update:  # skip the one we're actually analysing, this should be already done in the modifier-map creation...
                    constraints = cache.z3_modifier_constraints[modifier]
                    solver.add([const.translate(ctx) for const in constraints])
    
        conditionchanged_constraintset, additionals = cache.z3_conditionchanged_constraintsets[influence_update]
        solver.add([a.translate(ctx) for a in additionals])
    
        min_dt, label = get_behaviour_change_dt_from_constraintset(solver, conditionchanged_constraintset, z3_vars['dt'], ctx=ctx)
        if min_dt is not None:
            logger.info(f"Minimum condition change times in '{influence_update._name}' in entity '{influence_update._parent._name}' ({influence_update._parent.__class__.__name__}) is {min_dt} (at label {label})")
            ret = (to_python(min_dt), influence_update, label)
            all_dts.append( ret )

    def get_transition_time(self, transition, all_dts, ctx, cache):
        """
        - we need to find a solution for the guard condition (e.g. using a theorem prover)
        - guards are boolean expressions over port values
        - ports are influenced by Influences starting at other ports (find recursively)
        """
        logger.debug(f"Calculating the transition time of '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__})")
        solver = z3.Optimize(ctx)
    
        # find the ports that influence the transition
        transition_ports = SH.get_accessed_ports(transition.guard, transition)
        # logger.debug(f"The transitions influencing ports are called: {[p._name for p in transition_ports]}")
        # build a mapping that shows the propagation of information to the guard (what influences the guard)
        modifier_map = self.get_modifier_map(transition_ports)
    
        z3_vars = {}
        for port, value in cache.z3_vars.items():
            if port == "dt":
                z3_vars[port] = value.translate(ctx)
            elif isinstance(port, model.Port):
                portname = port._name
                portname_with_parent = port._parent._name + "." + port._name
                
                variable = value[port._name].translate(ctx)
                pre_var = value[port._name + "_0"].translate(ctx)
                
                z3_vars[port] = {
                    portname: variable,
                    portname_with_parent: variable,
                    portname + "_0": pre_var,
                    portname_with_parent + "_0": pre_var,
                    portname + ".pre": pre_var,
                    portname_with_parent + ".pre": pre_var,
                }
            else:
                logger.error(f"Don't know what to do with {port} {type(port)}. Value = \n {value}")
                raise ValueError("cannot translate port to context")

        solver.add(z3_vars['dt'] >= 0)
    
        # add the initial values for the sources of the dataflow
        for port, modifiers in modifier_map.items():
            # set default port value to the current value
            pre_value = get_z3_value(port, port._name + "_0").translate(ctx)
            solver.add(z3_vars[port][port._name + "_0"] == pre_value)
            if len(modifiers) == 0:
                solver.add(z3_vars[port][port._name] == z3_vars[port][port._name + "_0"])
    
        # create the constraints for updates and influences
        for port, modifiers in modifier_map.items():
            for modifier in modifiers:
                constraints = cache.z3_modifier_constraints[modifier]
                solver.add([const.translate(ctx) for const in constraints])
    
        guard_constraint = [guardconst.translate(ctx) for guardconst in cache.z3_modifier_constraints[transition] if not isinstance(guardconst, bool)]
        # this is because we cannot add booleans directly to a z3.Optimize (it works for Solver)
        # the issue is here:  https://github.com/Z3Prover/z3/issues/1736
        if isinstance(guard_constraint, bool):
            guard_constraint = z3.And(guard_constraint, ctx)
            
        solver.add(guard_constraint)
    
        objective = solver.minimize(z3_vars['dt'])  # find minimal value of dt
        check = solver.check()
        # logger.debug("satisfiability: %s", check)
        if solver.check() == z3.sat:
            log_if_level(logging.INFO, f"Minimum time to enable transition '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__}) will be enabled in {to_python(objective.value())}")
            # return (objective.value(), transition, as_epsilon_expr)
            inf_coeff, numeric_coeff, eps_coeff = objective.lower_values()
            ret = (Epsilon(numeric_coeff, eps_coeff), transition)
            all_dts.append( ret )
        elif check == z3.unknown:
            log_if_level(logging.WARNING, f"The calculation of the minimum transition time for '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__}) was UNKNOWN. This usually happening in the presence of non-linear constraints. Do we have any?")
            std_solver = z3.Solver()
            std_solver.add(solver.assertions())
            std_solver_check = std_solver.check()
            if std_solver_check == z3.sat:
                min_dt = std_solver.model()[z3_vars['dt']]
                log_if_level(logging.INFO, f"We did get a solution using the standard solver though: {to_python(min_dt)} Assuming that this is the smallest solution. CAREFUL THIS MIGHT BE WRONG (especially when the transition is an inequality constraint)!!!")
                ret = (to_python(min_dt), transition)
                all_dts.append( ret )
            elif std_solver_check == z3.unknown:
                logger.error(f"The standard solver was also not able to decide if there is a solution or not. The constraints are too hard!!!")
            else:
                logger.info("The standard solver says there is no solution to the constraints. This means we also couldn't minimize. Problem solved.")
        else:
            logger.debug(f"Constraint set to enable transition '{transition._name}' in entity '{transition._parent._name}' ({transition._parent.__class__.__name__}) is unsatisfiable.")

