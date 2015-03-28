from functools import wraps
import inspect

__author__ = 'Aleksander Chrabąszcz'


__registry = {}


class EntityPropertyException(Exception):
    pass


def get_method(name):
    return __registry[name]


def property_method(function):
    function.property_method = True
    return function


def property_class(clazz):
    for cls in inspect.getmro(clazz):
        for attr in cls.__dict__.values():
            if hasattr(attr, "__call__") and hasattr(attr, "property_method"):
                def check_property(fun, prop_name):
                    def inner(entity, *args, **kwargs):
                        if not entity.has_property(prop_name):
                            raise EntityPropertyException(str(entity.id) + " has no property " + prop_name)
                        return fun(entity, *args, **kwargs)
                    return inner
                __registry[attr.__name__] = check_property(attr, cls.__property__)
    return clazz


class PropertyType:
    __property__ = None


@property_class
class TakeablePropertyType(PropertyType):
    __property__ = "Takeable"

    @property_method
    def take_by(self, character):
        pass


print("metody: ", __registry)