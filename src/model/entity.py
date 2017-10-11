from copy import deepcopy, copy
from operator import attrgetter

from src.model.ports import Port, Input, Output, Local, LocalConst
from src.model.model import State, Transition, Influence, Update

class MetaEntity(type):
    pass

class Entity(metaclass=MetaEntity):

    def __new__(cls, *args, **kwargs):
        newobj = super().__new__(cls)

        copymap = dict() # dict of pairs {name: (new_object, old_object)}
        def getcopy(attrname, original_object, deep_copy=False):
            if attrname not in copymap:
                new_object = deepcopy(original_object) if deep_copy else copy(original_object)
                copymap[attrname] = new_object
                copymap[original_object] = new_object
            return copymap[attrname] # return the new one

        def get_local_attribute(identifier):
            if isinstance(identifier, str):
                return attrgetter(identifier)(newobj)
            else:
                return copymap[identifier]


        """ copy ports (shallow copy, because they reference resources, which are unique) """
        for name, port in get_ports(newobj, as_dict=True).items():
            newport = getcopy(name, port, deep_copy=False)
            setattr(newobj, name, newport)

        """ copy states (deep copy) """
        for name, state in get_states(newobj, as_dict=True).items():
            if name == "current": continue # skip current state

            newstate = getcopy(name, state, deep_copy=True)
            setattr(newobj, name, newstate)

        """ we treat "current" specially """
        if hasattr(newobj, "current"):
            setattr(newobj, "current", copymap[newobj.current])

        """ copy Entities (deep copy) """
        for name, entity in get_entities(newobj, as_dict=True).items():
            newentity = getcopy(name, entity, deep_copy=True)
            setattr(newobj, name, newentity)

        """ get transitions and adapt them """
        for name, trans in get_transitions(newobj, as_dict=True).items():
            source = get_local_attribute(trans.source)
            target = get_local_attribute(trans.target)
            newtransition = Transition(source=source, target=target, guard=trans.guard)
            setattr(newobj, name, newtransition)

        """ get updates and adapt them """
        for name, update in get_updates(newobj, as_dict=True).items():
            state = get_local_attribute(update.state)
            newupdate = Update(state=state, function=update.function)
            setattr(newobj, name, newupdate)

        """ get influences and adapt them """
        for name, influence in get_influences(newobj, as_dict=True).items():
            source = get_local_attribute(influence.source)
            target = get_local_attribute(influence.target)
            newinfluence = Influence(source=source, target=target, function=influence.function)
            setattr(newobj, name, newinfluence)

        return newobj

class LogicalEntity(Entity):
    pass

""" get_X_from_entity functions"""

def get_states(entity, as_dict=False):
    return get_by_klass(entity, State, as_dict)

def get_inputs(entity, as_dict=False):
    return get_by_klass(entity, Input, as_dict)

def get_outputs(entity, as_dict=False):
    return get_by_klass(entity, Output, as_dict)

def get_locals(entity, as_dict=False):
    return get_by_klass(entity, Local, as_dict)

def get_ports(entity, as_dict=False):
    return get_by_klass(entity, Port, as_dict)

def get_entities(entity, as_dict=False):
    return get_by_klass(entity, Entity, as_dict)

def get_updates(entity, as_dict=False):
    return get_by_klass(entity, Update, as_dict)

def get_transitions(entity, as_dict=False):
    return get_by_klass(entity, Transition, as_dict)

def get_influences(entity, as_dict=False):
    return get_by_klass(entity, Influence, as_dict)

def get_by_klass(entity, klass, as_dict=False):
    if as_dict:
        attrs = {attr: get_dict_attr(entity, attr) for attr in dir(entity)}
        dbg = {name: attr for (name, attr) in attrs.items() if isinstance(attr, klass)}
        return dbg
    else:
         attrs = [get_dict_attr(entity, attr) for attr in dir(entity)]
         return list(set([attr for attr in attrs if isinstance(attr, klass)]))

def get_dict_attr(obj, attr):
    for obj in [obj] + obj.__class__.mro():
        if attr in obj.__dict__:
            return obj.__dict__[attr]
    raise AttributeError("object {} doesn't have attribute '{}'".format(obj, attr))