import inspect

import copy
import project_root
import wrapt
from exeris.core import main, models
from exeris.core.main import db


def object_import(name):
    parts = name.split('.')
    module = ".".join(parts[:-1])
    m = __import__(module)
    for comp in parts[1:]:
        m = getattr(m, comp)
    return m


def call(json_to_call, **injected_args):
    """
    Call the list which is the class-arguments pair.
    :param json_to_call: list being: [full path string to class, dict of arguments to call class' constructor]
    :param injected_args: Additional args to add to specified json args. Injected args can overwrite values from json
    :return: instance of class specified in json constructed with specified args
    """
    if not isinstance(json_to_call, list):
        raise AssertionError("'{}' is not a list to be called".format(json_to_call))

    function_id, kwargs = json_to_call[0], json_to_call[1]

    func = object_import(function_id)
    kwargs = copy.deepcopy(kwargs)
    kwargs.update(injected_args)

    return func(**kwargs)


def get_qualified_class_name(cls):
    class_module = inspect.getfile(cls)
    path_in_project = project_root.relative_to_project_root(class_module)
    module_path = path_in_project.replace("/", ".").strip(".").replace(".py", "")
    full_qualified_name = module_path + "." + cls.__qualname__
    return full_qualified_name


def get_qualified_name(obj):
    def remove_last_part(text):
        return "/".join(text.split("/")[:-1])

    class_module_path = inspect.getmodule(obj).__file__
    path_in_project = project_root.relative_to_project_root(class_module_path)
    module_path = path_in_project.replace("/", ".").strip(".").replace(".py", "")
    full_qualified_name = module_path + "." + obj.__class__.__qualname__
    return full_qualified_name


def serialize(obj):
    full_qualified_name = get_qualified_name(obj)

    inspected_init_args = inspect.getfullargspec(obj.__class__.__init__).args

    inspected_init_args.pop(0)  # remove 'self'

    args_to_serialize = {}
    for arg_name in inspected_init_args:
        arg_value_to_serialize = getattr(obj, arg_name)

        from exeris.core import actions
        # need to be subject of argument type conversion as specified by 'convert' decorator
        if isinstance(arg_value_to_serialize, models.Entity):
            arg_value_to_serialize = arg_value_to_serialize.id
        elif isinstance(arg_value_to_serialize, models.EntityType):
            arg_value_to_serialize = arg_value_to_serialize.name
        elif isinstance(arg_value_to_serialize, actions.AbstractAction):
            arg_value_to_serialize = serialize(arg_value_to_serialize)

        args_to_serialize[arg_name] = arg_value_to_serialize

    return [full_qualified_name, args_to_serialize]


def convert(**argument_types):
    @wrapt.decorator
    def wrapper(wrapped, instance, args, kwargs):
        converted_args = {}

        for arg_name, arg_value in kwargs.items():

            def arg_exists(arg_name):
                return arg_name in argument_types and argument_types[arg_name] is not None

            from exeris.core import actions
            if arg_exists(arg_name) and issubclass(argument_types[arg_name], db.Model) \
                    and type(arg_value) in (int, str):
                converted_args[arg_name] = argument_types[arg_name].query.get(arg_value)
            elif arg_exists(arg_name) and issubclass(argument_types[arg_name], actions.AbstractAction) \
                    and isinstance(arg_value, list):
                converted_args[arg_name] = call(arg_value)  # recursively deserialize JSON into python Action object
            else:
                converted_args[arg_name] = arg_value

        return wrapped(*args, **converted_args)

    return wrapper


def perform_or_turn_into_intent(executor, action, priority=1):
    """
    Method tries to execute `perform` method on specified action.
    If it doesn't succeed and result in  throwing on throwing exception of specified class, then
    action is serialized and turned into EntityIntention for specified entity.
    If any unlisted exception is raised then it's propagated.
    It means the action needs to be serializable and deserializable.
    :param executor: entity (not only Character) being actor (executor) for this action. Used in intent.
    :param action: action that is tried to be performed
    :param priority: int, used in intent. Intents with higher prio are handled earlier (it matters especially
        when there's limited number of intents to complete at the time).
    """

    db.session.begin_nested()
    try:
        result = action.perform()
        db.session.commit()  # commit savepoint
        return result
    except main.TurningIntoIntentExceptionMixin as exception:
        db.session.rollback()  # rollback to savepoint

        models.Intent.query.filter_by(executor=executor).delete()  # TODO no multiple intents till #72

        exception.turn_into_intent(executor, action, priority)
