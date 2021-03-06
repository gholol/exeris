import collections
import datetime
import logging

import geoalchemy2 as gis
import sqlalchemy as sql
import sqlalchemy.dialects.postgresql as psql
import sqlalchemy.orm
from flask_security import UserMixin, RoleMixin
from geoalchemy2.shape import to_shape, from_shape
from shapely.geometry import Point
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method

import sqlalchemy_json_mutable
from exeris.core import main, util
from exeris.core.main import db, Types, Events, PartialEvents
from exeris.core.map_data import MAP_HEIGHT, MAP_WIDTH
from exeris.core.properties_base import P

# subclasses hierarchy for Entity
ENTITY_BASE = "base"
ENTITY_ITEM = "item"
ENTITY_LOCATION = "location"
ENTITY_ROOT_LOCATION = "root_location"
ENTITY_PASSAGE = "passage"
ENTITY_CHARACTER = "character"
ENTITY_ACTIVITY = "activity"
ENTITY_TERRAIN_AREA = "terrain_area"
ENTITY_GROUP = "group"
ENTITY_COMBAT = "combat"
ENTITY_BURIED_CONTENT = "buried_content"

TYPE_NAME_MAXLEN = 32
TAG_NAME_MAXLEN = 32
PLAYER_ID_MAXLEN = 24
ALIVE_CHARACTER_WEIGHT = 1000

logger = logging.getLogger(__name__)


def ids(entities):
    return [entity.id for entity in entities]


roles_users = db.Table('player_roles',
                       db.Column('player_id', db.String(PLAYER_ID_MAXLEN), db.ForeignKey('players.id')),
                       db.Column('role_id', db.Integer, db.ForeignKey('roles.id')),
                       db.Index("player_roles_index", "player_id", "role_id"))


class Role(db.Model, RoleMixin):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))


class Player(db.Model, UserMixin):
    __tablename__ = "players"

    id = sql.Column(sql.String(PLAYER_ID_MAXLEN), primary_key=True)

    email = sql.Column(sql.String(32), unique=True)
    language = sql.Column(sql.String(2), default="en")
    register_date = sql.Column(sql.DateTime)
    register_game_date = sql.Column(sql.BigInteger)
    password = sql.Column(sql.String)

    active = db.Column(db.Boolean, index=True)
    roles = db.relationship('Role', secondary=roles_users,
                            backref=db.backref('players', lazy='dynamic'))
    confirmed_at = sql.Column(sql.DateTime)

    serial_id = sql.Column(sql.Integer, sql.Sequence('player_serial_id'), nullable=False)

    def __init__(self, id, email, language, password, active=True, register_date=None, register_game_date=None,
                 **kwargs):
        self.id = id
        self.email = email
        self.language = language
        self.password = password

        self.active = active
        self.register_date = register_date if register_date else datetime.datetime.now()

        from exeris.core import general
        self.register_game_date = register_game_date if register_game_date else general.GameDate.now()

    @sql.orm.validates("register_game_date")
    def validate_register_game_date(self, key, register_game_date):
        return register_game_date.game_timestamp

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @hybrid_property
    def alive_characters(self):
        return Character.query.filter_by(player=self).filter(Character.is_alive).all()

    def get_id(self):
        return self.id

    @classmethod
    def by_id(cls, player_id):
        return cls.query.get(player_id)


class TranslatedText(db.Model):
    __tablename__ = "translations"

    def __init__(self, name, language, content, form=""):
        self.name = name
        self.language = language
        self.content = content
        self.form = form

    name = sql.Column(sql.String(64), primary_key=True)
    language = sql.Column(sql.String(8), primary_key=True)
    content = sql.Column(sql.String)
    form = sql.Column(sql.String(8))


class EntityType(db.Model):
    __tablename__ = "entity_types"

    name = sql.Column(sql.String(32), primary_key=True)  # no spaces allowed

    def __init__(self, name):
        self.name = name

    properties = sql.orm.relationship("EntityTypeProperty", back_populates="type")

    discriminator_type = sql.Column(sql.String(15))  # discriminator

    @hybrid_property
    def parent_groups(self):
        return [parent_group.parent for parent_group in self._parent_groups_junction]

    @classmethod
    def by_name(cls, type_name):
        return cls.query.get(type_name)

    def contains(self, entity_type):
        return entity_type == self  # is member of "itself" group

    def get_descending_types(self):
        return [(self, 1.0)]

    def get_property(self, name):
        type_property = EntityTypeProperty.query.filter_by(type=self, name=name).first()
        if type_property:
            return type_property.data
        return None

    @hybrid_method
    def has_property(self, name, **kwargs):
        prop = self.get_property(name)
        if prop is None:
            return False
        for key, value in kwargs.items():
            if key not in prop or prop[key] != value:
                return False
        return True

    @has_property.expression
    def has_property(self, name, **kwargs):
        if not kwargs:
            return sql.select([True]) \
                .where(sql.and_(EntityTypeProperty.name == name,
                                EntityTypeProperty.type_name == self.name)) \
                .label("property_exists")
        else:
            entity_type_query_parts = []
            for key, value in kwargs.items():
                cast_value = util.Sql.cast_json_value_to_psql_type(EntityTypeProperty.data[key], value)
                entity_type_query_parts += [cast_value == value]
            return sql.select([True]) \
                .where(sql.and_(EntityTypeProperty.name == name,
                                EntityTypeProperty.type_name == self.name,
                                *entity_type_query_parts)) \
                .label("property_exists")

    def key_value_pair_exists(self, key, value, kv_dict):
        return key in kv_dict and kv_dict[key] == value

    __mapper_args__ = {
        "polymorphic_identity": ENTITY_BASE,
        "polymorphic_on": discriminator_type,
    }

    def __repr__(self):
        return "{{EntityType, name: {}}}".format(self.name)


class TypeGroupElement(db.Model):
    __tablename__ = "entity_group_elements"

    def __init__(self, child, efficiency=1.0):
        self.child = child
        self.efficiency = efficiency

    parent_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey('entity_type_groups.name'), primary_key=True)
    parent = sql.orm.relationship("TypeGroup", foreign_keys=[parent_name], backref="_children_junction")
    child_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey('entity_types.name'), primary_key=True)
    child = sql.orm.relationship("EntityType", foreign_keys=[child_name], backref="_parent_groups_junction")
    efficiency = sql.Column(sql.Float, default=1.0, nullable=False)
    # holds quantity efficiency for stackables and quality efficiency for non-stackables


class ItemType(EntityType):
    __tablename__ = "item_types"

    name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), primary_key=True)

    def __init__(self, name, unit_weight, portable=True, stackable=False):
        super().__init__(name)
        self.unit_weight = unit_weight
        self.portable = portable
        self.stackable = stackable

    unit_weight = sql.Column(sql.Integer)
    stackable = sql.Column(sql.Boolean)
    portable = sql.Column(sql.Boolean)

    def quantity_efficiency(self, entity_type):  # quack quack
        if entity_type == self:
            return 1.0
        raise ValueError

    def __repr__(self):
        return "{{ItemType, name: {}}}".format(self.name)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_ITEM,
    }


class TypeGroup(EntityType):
    __tablename__ = "entity_type_groups"

    name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), primary_key=True)
    stackable = sql.Column(sql.Boolean)

    def __init__(self, name, stackable=False):
        super().__init__(name)
        self.stackable = stackable

    @hybrid_property
    def children(self):
        return [group_element.child for group_element in self._children_junction]

    def add_to_group(self, child, efficiency=1.0):
        self._children_junction.append(TypeGroupElement(child, efficiency))

    def remove_from_group(self, child):
        self._children_junction.remove(TypeGroupElement.query.filter_by(parent=self, child=child).one())

    def contains(self, entity_type):
        return not not self.get_group_path(entity_type)

    def get_descending_types(self):
        """
        Returns a list of tuples which represent all concrete EntityTypes contained by this group.
        The first element of the pair is EntityType, the second element is float representing its overall efficiency
        """
        result = []
        for type_group_element in self._children_junction:
            if isinstance(type_group_element.child, TypeGroup):
                pairs = type_group_element.child.get_descending_types()
                result += [(type, efficiency * type_group_element.efficiency) for type, efficiency in pairs]
            else:
                result.append((type_group_element.child, type_group_element.efficiency))
        return result

    def get_group_path(self, entity_type):
        """
        Recursively searching for entity_type in groups' children.
        If found, it returns a list of nodes which need to be visited to get from 'self' to 'entity_type'
        If not found, returns an empty list
        """
        if entity_type in self.children:
            return [self, entity_type]
        child_groups = filter(lambda group: isinstance(group, TypeGroup), self.children)
        for group in child_groups:
            path = group.get_group_path(entity_type)
            if path:
                return [self] + path
        return []

    def quantity_efficiency(self, entity_type):
        if not self.stackable:
            return 1.0

        lst = self.get_group_path(entity_type)
        pairs = zip(lst[:-1], lst[1:])
        efficiency = 1.0
        for pair in pairs:
            efficiency *= TypeGroupElement.query.filter_by(parent=pair[0], child=pair[1]).one().efficiency
        return efficiency

    def quality_efficiency(self, entity_type):
        if self.stackable:
            return 1.0

        lst = self.get_group_path(entity_type)
        pairs = zip(lst[:-1], lst[1:])
        efficiency = 1.0
        for pair in pairs:
            efficiency *= TypeGroupElement.query.filter_by(parent=pair[0], child=pair[1]).one().efficiency
        return efficiency

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_GROUP,
    }

    def __repr__(self):
        return "{TypeGroup, name: " + self.name + "}"


def get_concrete_types_for_groups(groups):
    """Returns a set of concrete entity types which are contained by any of the specified groups"""
    concrete_types = set()
    for group in groups:
        concrete_types.update([entity_type for entity_type, efficiency in group.get_descending_types()])
    return concrete_types


def clamp_to_0_1(states):
    for state, value in states.items():
        if state in main.NORMALIZED_STATES:
            if value < 0 or value > 1:
                states[state] = util.clamp_0_1(value)


class Entity(db.Model):
    """
    Abstract base for all entities in the game, like items or locations
    """
    __tablename__ = "entities"

    ROLE_BEING_IN = 1
    ROLE_USED_FOR = 2

    id = sql.Column(sql.Integer, primary_key=True)

    def __init__(self):
        self.states = {
            main.States.DAMAGE: 0,
            main.States.MODIFIERS: {},
        }

        self.add_type_specific_states()
        self.states.listeners.append(clamp_to_0_1)
        self.states.listeners.append(create_death_listener(self))

    def add_type_specific_states(self):
        states_type_property = EntityTypeProperty.query.get((self.type.name, P.STATES))
        if states_type_property:
            self._add_initial_states_to_states(states_type_property)

    def _add_initial_states_to_states(self, states_type_property):
        for state, state_prop in states_type_property.data.items():
            if state not in self.states:
                self.states[state] = state_prop["initial"]

    weight = sql.Column(sql.Integer)

    parent_entity_id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), nullable=True)
    parent_entity = sql.orm.relationship(lambda: Entity, primaryjoin=parent_entity_id == id,
                                         foreign_keys=parent_entity_id, remote_side=id, uselist=False)
    role = sql.Column(sql.SmallInteger, nullable=True)

    __table_args__ = (sql.Index("parent_entity_role_index", "parent_entity_id", "role", "discriminator_type"),)

    title = sql.Column(sql.String, nullable=True)
    properties = sql.orm.relationship("EntityProperty", back_populates="entity",
                                      cascade="all, delete, delete-orphan")

    states = sql.Column(sqlalchemy_json_mutable.JsonDict, index=True)  # index for items to be deleted

    @hybrid_property
    def being_in(self):
        if self.role != Entity.ROLE_BEING_IN:
            return None
        return self.parent_entity

    @being_in.setter
    def being_in(self, parent_entity):
        self.parent_entity = parent_entity
        self.role = Entity.ROLE_BEING_IN

    @being_in.expression
    def being_in(cls):
        return cls.parent_entity
        # print(select(cls.id).where((cls.role == Item.ROLE_BEING_IN) & (cls.parent_entity == Item)))
        # return select(cls.parent_entity).where((cls.role == Entity.ROLE_BEING_IN) & (cls.parent_entity_id == Entity.id))
        # return case([(cls.role == Entity.ROLE_BEING_IN, cls.parent_entity_id)], else_=-1)
        # return select([cls.parent_entity]).where(cls.role == Entity.ROLE_BEING_IN).as_scalar()
        # return func.IF(cls.role == Entity.ROLE_BEING_IN, Entity.parent_entity, None)

    @hybrid_method
    def is_in(self, parents):
        if not isinstance(parents, collections.Iterable):
            parents = [parents]
        return self.role == Entity.ROLE_BEING_IN and (self.parent_entity in parents)

    @is_in.expression
    def is_in(self, parents):
        if not isinstance(parents, collections.Iterable):
            parents = [parents]
        if any([e_id is None for e_id in ids(parents)]):  # any id is missing
            db.session.flush()
        return (self.role == Entity.ROLE_BEING_IN) & (
            self.parent_entity_id.in_(ids(parents)) & ~self.discriminator_type.in_([ENTITY_LOCATION,
                                                                                    ENTITY_ROOT_LOCATION]))

    @hybrid_method
    def is_used_for(self, parents):
        if not isinstance(parents, collections.Iterable):
            parents = [parents]
        if any([e_id is None for e_id in ids(parents)]):  # any id is missing
            db.session.flush()
        return (self.parent_entity_id.in_(ids(parents))) & (self.role == Entity.ROLE_USED_FOR)

    @hybrid_property
    def used_for(self):
        if self.role == Entity.ROLE_BEING_IN:
            return None
        return self.parent_entity

    @used_for.setter
    def used_for(self, parent_entity):
        self.parent_entity = parent_entity
        self.role = Entity.ROLE_USED_FOR

    def alter_type(self, new_type):
        self.type = new_type
        self.add_type_specific_states()

    def get_property(self, name):
        props = {}
        ok = False
        type_property = EntityTypeProperty.query.filter_by(type=self.type, name=name).first()
        if type_property:
            props.update(type_property.data)
            ok = True

        entity_property = EntityProperty.query.filter_by(entity=self, name=name).first()
        if entity_property:
            props.update(entity_property.data)
            ok = True

        if not ok:
            return None
        return props

    def get_entity_property(self, name):
        return EntityProperty.query.filter_by(entity=self, name=name).first()

    @hybrid_method
    def has_property(self, name, **kwargs):
        prop = self.get_property(name)
        if prop is None:
            return False
        for key, value in kwargs.items():
            if key not in prop or prop[key] != value:
                return False
        return True

    @has_property.expression
    def has_property(cls, name, **kwargs):
        if not kwargs:
            cls_subquery = sql.orm.aliased(cls)
            return db.session.query(cls_subquery.id) \
                .filter(cls.id == cls_subquery.id) \
                .outerjoin(EntityProperty, sql.and_(EntityProperty.entity_id == cls_subquery.id,
                                                    EntityProperty.name == name)) \
                .outerjoin(EntityTypeProperty, sql.and_(EntityTypeProperty.type_name == cls_subquery.type_name,
                                                        EntityTypeProperty.name == name)) \
                .filter(
                sql.or_(
                    EntityProperty.name.isnot(None),
                    EntityTypeProperty.name.isnot(None)
                )).correlate(cls).exists()
        else:
            entity_or_type_query_parts = []
            for key, value in kwargs.items():
                entity_cast_value = util.Sql.cast_json_value_to_psql_type(EntityProperty.data[key], value)
                type_cast_value = util.Sql.cast_json_value_to_psql_type(EntityTypeProperty.data[key], value)

                entity_or_type_query_parts += [sql.or_(
                    entity_cast_value == value,
                    sql.and_(
                        type_cast_value == value,
                        sql.sql.functions.coalesce(entity_cast_value == value, True)
                    )
                )]

                cls_subquery = sql.orm.aliased(cls)
            return db.session.query(cls_subquery.id) \
                .filter(cls.id == cls_subquery.id) \
                .outerjoin(EntityProperty, sql.and_(EntityProperty.entity_id == cls_subquery.id,
                                                    EntityProperty.name == name)) \
                .outerjoin(EntityTypeProperty, sql.and_(EntityTypeProperty.type_name == cls_subquery.type_name,
                                                        EntityTypeProperty.name == name)) \
                .filter(sql.and_(*entity_or_type_query_parts)).correlate(cls).exists()

    @hybrid_property
    def damage(self):
        return self.states[main.States.DAMAGE]

    @damage.setter
    def damage(self, new_value):
        self.states[main.States.DAMAGE] = new_value

    @hybrid_property
    def modifiers(self):
        return self.states[main.States.MODIFIERS]

    def remove(self):
        parent_entity = self.being_in

        self.parent_entity = None

        entities_inside = Entity.query.filter(Entity.is_in(self)).all()
        for entity in entities_inside:
            logger.debug("Removing %s which is inside of removed entity: %s", entity, self)
            entity.being_in = None

        db.session.delete(self)

        main.call_hook(main.Hooks.ENTITY_CONTENTS_COUNT_DECREASED, entity=parent_entity)

    def alter_property(self, name, data=None):
        """
        Creates an EntityProperty for this Entity if it doesn't exist and fills it with provided data
        or REPLACES the data of existing EntityProperty with provided data.
        It doesn't affect any EntityTypeProperty for this Entity's type.
        :param name: name of the property.
        :param data: dict with data for this property
        """
        if not data:
            data = {}
        entity_property = EntityProperty.query.filter_by(entity=self, name=name).first()
        if entity_property:
            entity_property.data = data
        else:
            self.properties.append(EntityProperty(name, data=data))

    def is_empty(self, excluding=None):
        """
        Checks if there's anything inside (being_in) or directly neighbouring of this entity.
        It yields correct results for any entity type excluding Activity.
        For Location it says whether location has any neighbour (including parent).
        It may not yield correct result for Activity (items used in activity have USED_FOR role).
        :param excluding: list of entities which are not taken into account
        :return: True if this entity stores any entity inside or has any neighbour
        """
        excluding = ids(excluding if excluding else [])
        excluding.append(-1)  # to avoid empty IN() contradiction
        if isinstance(self, RootLocation):
            if Passage.query.filter(Passage.incident(self)) \
                    .filter(~Passage.left_location_id.in_(excluding)) \
                    .filter(~Passage.right_location_id.in_(excluding)) \
                    .count():
                return False
        return not Entity.query.filter(Entity.is_in(self)).filter(~Entity.id.in_(excluding)).count()

    def has_activity(self):
        return Activity.query.filter(Activity.is_in(self)).count() > 0

    def get_position(self):
        return self.get_root().position

    def parent_locations(self):
        return self.being_in.parent_locations()

    def get_location(self):
        return self._get_parent_of_class(Location)

    def get_root(self):
        return self._get_parent_of_class(RootLocation)

    def _get_parent_of_class(self, entity_class):
        if isinstance(self, entity_class):
            return self
        else:
            return self.being_in._get_parent_of_class(entity_class)

    def pyslatize(self, **overwrites):
        pyslatized = dict(entity_type=ENTITY_BASE, entity_id=self.id)
        if self.has_property(P.DYNAMIC_NAMEABLE):
            pyslatized["dynamic_nameable"] = True
        return dict(pyslatized, **overwrites)

    discriminator_type = sql.Column(sql.String(15))  # discriminator

    __mapper_args__ = {
        "polymorphic_identity": ENTITY_BASE,
        "polymorphic_on": discriminator_type,
    }

    @classmethod
    def by_id(cls, entity_id):
        return cls.query.get(entity_id)

    def __repr__(self):
        return str(self.__class__) + str(self.__dict__)


@sqlalchemy.event.listens_for(Entity, "load", propagate=True)
def clamp_states_to_0_1(target, _):
    target.states = sqlalchemy_json_mutable.mutable_types.NestedMutableDict.coerce("states", target.states)
    target.states.listeners.append(clamp_to_0_1)


class Intent(db.Model):
    """
    Represents entity's will or plan to perform certain action (which can be impossible at the moment)
    """
    __tablename__ = "intents"

    id = sql.Column(sql.Integer, primary_key=True, autoincrement=True)

    def __init__(self, executor, intent_type, priority, target, serialized_action):
        self.executor = executor
        self.type = intent_type
        self.priority = priority
        self.target = target
        self.serialized_action = serialized_action

    executor_id = sql.Column(sql.Integer, sql.ForeignKey(Entity.id), primary_key=True)
    executor = sql.orm.relationship(Entity, uselist=False, backref="intents", foreign_keys=executor_id)

    type = sql.Column(sql.String(20), index=True)
    priority = sql.Column(sql.Integer)

    target_id = sql.Column(sql.Integer, sql.ForeignKey(Entity.id, ondelete="CASCADE"), nullable=True, index=True)
    target = sql.orm.relationship(Entity, uselist=False, foreign_keys=target_id)

    serialized_action = sql.Column(sqlalchemy_json_mutable.JsonList)  # single action

    def __enter__(self):
        from exeris.core import deferred
        self._value = deferred.call(self.serialized_action)
        return self._value

    def __exit__(self, type, value, traceback):
        from exeris.core import deferred
        self.serialized_action = deferred.serialize(self._value)

    def __repr__(self):
        return "{{Intent, executor: {}, type: {}, target: {}, action: {}}}".format(self.executor, self.type,
                                                                                   self.target, self.serialized_action)


class LocationType(EntityType):
    __tablename__ = "location_types"

    name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), primary_key=True)

    def __init__(self, name, base_weight):
        super().__init__(name)
        self.base_weight = base_weight

    base_weight = sql.Column(sql.Integer)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_LOCATION,
    }


def create_death_listener(self):
    def listen_for_death(states):
        if states[main.States.DAMAGE] >= 1.0:
            main.call_hook(main.Hooks.DAMAGE_EXCEEDED, entity=self)

    return listen_for_death


class Character(Entity):
    __tablename__ = "characters"

    SEX_MALE = "m"
    SEX_FEMALE = "f"

    DEATH_STARVATION = "starvation"
    DEATH_WEAPON = "weapon"
    DEATH_ILLNESS = "illness"

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)

    def __init__(self, name, sex, player, language, spawn_date, spawn_position, being_in):

        self.being_in = being_in
        self.name = name
        self.sex = sex
        self.player = player

        self.language = language

        self.spawn_position = spawn_position
        self.spawn_date = spawn_date

        self.type = EntityType.by_name(Types.ALIVE_CHARACTER)
        self.weight = ALIVE_CHARACTER_WEIGHT
        super().__init__()
        self.properties.append(EntityProperty(P.PREFERRED_EQUIPMENT))

    sex = sql.Column(sql.Enum(SEX_MALE, SEX_FEMALE, name="sex"))

    player_id = sql.Column(sql.String(PLAYER_ID_MAXLEN), sql.ForeignKey('players.id'), index=True)
    player = sql.orm.relationship(Player, uselist=False)

    language = sql.Column(sql.String(2))

    spawn_date = sql.Column(sql.BigInteger)
    spawn_position = sql.Column(gis.Geometry("POINT"))

    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), index=True)
    type = sql.orm.relationship(EntityType, uselist=False)

    eating_queue = sql.Column(sqlalchemy_json_mutable.JsonDict, default=lambda x: {})

    @hybrid_property
    def name(self):
        own_name = ObservedName.query.filter_by(target=self, observer=self).first()
        if own_name:
            return own_name.name
        return "UNNAMED"

    @name.setter
    def name(self, value):
        if self.id is not None:
            observed_name = ObservedName.query.filter_by(target=self, observer=self).first()
            if observed_name:
                observed_name.name = value
                return
        db.session.add(ObservedName(self, self, value))

    def has_access(self, entity, rng=None):
        from exeris.core import general
        if not rng:
            rng = general.InsideRange()
        return rng.is_near(self, entity)

    @hybrid_property
    def is_alive(self):
        return self.type_name == Types.ALIVE_CHARACTER

    FOOD_BASED_ATTR_INITIAL_VALUE = 0.1

    @sql.orm.validates("spawn_position")
    def validate_position(self, key, spawn_position):  # we assume position is a Polygon
        return from_shape(spawn_position)

    @sql.orm.validates("spawn_date")
    def validate_spawn_date(self, key, spawn_date):
        return spawn_date.game_timestamp

    def contents_weight(self):
        entities = Entity.query.filter(Entity.is_in(self)).all()
        return sum([entity.weight + entity.contents_weight() for entity in entities])

    def pyslatize(self, **overwrites):
        pyslatized = dict(entity_type=ENTITY_CHARACTER, character_id=self.id, character_gen=self.sex,
                          character_name=self.type_name)
        if self.has_property(P.DYNAMIC_NAMEABLE):
            pyslatized["dynamic_nameable"] = True
        return dict(pyslatized, **overwrites)

    def __repr__(self):
        return "{{Character name={},player={}}}".format(self.name, self.player_id)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_CHARACTER,
    }


@sqlalchemy.event.listens_for(Entity, "load", propagate=True)
def add_death_listener(target, _):
    target.states = sqlalchemy_json_mutable.mutable_types.NestedMutableDict.coerce("states", target.states)
    target.states.listeners.append(create_death_listener(target))


class Item(Entity):
    __tablename__ = "items"

    DAMAGED_LB = 0.7

    def __init__(self, item_type, parent_entity, *, weight=None, amount=None, role_being_in=True, quality=1.0):
        self.type = item_type

        if role_being_in:
            self.being_in = parent_entity
        else:
            self.used_for = parent_entity

        if weight is not None:
            self.weight = weight
        elif amount is not None:
            self.weight = amount * item_type.unit_weight
        else:
            self.weight = item_type.unit_weight
        self.quality = quality
        super().__init__()

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)

    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("item_types.name"), index=True)
    type = sql.orm.relationship(ItemType, uselist=False)

    visible_parts = sql.Column(sqlalchemy_json_mutable.JsonList, default=lambda x: [])  # sorted list of item type names

    @sql.orm.validates("visible_parts")
    def validate_visible_parts(self, key, visible_parts):
        if visible_parts is None:
            visible_parts = []
        # turn (optional) item types into names
        visible_parts = [part if isinstance(part, str) else part.name for part in visible_parts]
        return sorted(visible_parts)

    quality = sql.Column(sql.Float, default=1.0)

    @hybrid_property
    def amount(self):
        if not self.type.stackable:
            return 1
        return int(self.weight / self.type.unit_weight)

    @amount.setter
    def amount(self, new_amount):
        if not self.type.stackable:
            raise ValueError("it's impossible to alter amount for non-stackable")
        if new_amount > 0:
            self.weight = new_amount * self.type.unit_weight
        else:
            self.remove()

    def remove(self, move_contents=True):
        if move_contents:
            items_inside = Item.query.filter(Item.is_in(self)).all()

            for item in items_inside:
                item.being_in = self.being_in  # move outside

        super(Item, self).remove()

    def contents_weight(self):
        entities = Entity.query.filter(Entity.is_in(self)).all()
        return sum([entity.weight + entity.contents_weight() for entity in entities])

    def pyslatize(self, **overwrites):
        pyslatized = dict(entity_type=ENTITY_ITEM, item_id=self.id, item_name=self.type_name,
                          item_damage=self.damage)
        if self.type.stackable:
            pyslatized["item_amount"] = self.amount
        if self.title:
            pyslatized["item_title"] = self.title
        if self.visible_parts:
            pyslatized["item_parts"] = self.visible_parts
        prop = self.get_property(P.VISIBLE_MATERIAL)
        if prop:
            pyslatized["item_material"] = prop
        prop = self.get_property(P.HAS_DEPENDENT)
        if prop:
            pyslatized["item_dependent"] = prop["name"]
        domesticated_prop = self.get_property(P.DOMESTICATED)
        if domesticated_prop and "trusted" in domesticated_prop:
            pyslatized["trusted"] = domesticated_prop["trusted"]
        key_to_lock_prop = self.get_property(P.KEY_TO_LOCK)
        if key_to_lock_prop:
            pyslatized["unique_id"] = key_to_lock_prop["lock_id"]
        signature_prop = self.get_property(P.SIGNATURE)
        if signature_prop:
            pyslatized["unique_id"] = signature_prop["value"]
        lock_prop = self.get_property(P.LOCKABLE)
        if lock_prop and lock_prop.get("lock_exists", False):
            pyslatized["unique_id"] = lock_prop["lock_id"]
        return dict(pyslatized, **overwrites)

    def __repr__(self):
        if not self.parent_entity:
            logger.warn("Item with id=%s has no parent entity", self.id)
            return "{{Item id={}, type={}, NO PARENT ENTITY}}".format(self.id, self.type_name)
        if self.amount > 1:
            return "{{Item id={}, type={}, amount={}, parent={}, parent_type={}}}" \
                .format(self.id, self.type_name, self.amount, self.parent_entity.id,
                        self.parent_entity.discriminator_type)
        return "{{Item id={}, type={}, parent={}, parent_type={}}}" \
            .format(self.id, self.type_name, self.parent_entity.id, self.parent_entity.discriminator_type)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_ITEM,
    }


class Activity(Entity):
    __tablename__ = "activities"

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)

    def __init__(self, being_in, name_tag, name_params, requirements, ticks_needed, initiator):
        self.being_in = being_in

        self.name_tag = name_tag
        self.name_params = name_params

        self.requirements = requirements
        self.ticks_needed = ticks_needed
        self.ticks_left = ticks_needed
        self.initiator = initiator

        self.quality_ticks = 0.0
        self.quality_sum = 0

        self.type = EntityType.by_name(Types.ACTIVITY)
        self.weight = 0
        super().__init__()

    name_tag = sql.Column(sql.String(TAG_NAME_MAXLEN))
    name_params = sql.Column(sqlalchemy_json_mutable.JsonDict)

    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), index=True)
    type = sql.orm.relationship(EntityType, uselist=False)

    initiator_id = sql.Column(sql.Integer, sql.ForeignKey("characters.id"), index=True)
    initiator = sql.orm.relationship("Character", uselist=False, primaryjoin="Activity.initiator_id == Character.id",
                                     post_update=True)

    requirements = sql.Column(sqlalchemy_json_mutable.JsonDict)  # a dict of requirements
    result_actions = sql.Column(
        sqlalchemy_json_mutable.JsonList)  # a list of serialized constructors of subclasses of AbstractAction
    quality_sum = sql.Column(sql.Float)
    quality_ticks = sql.Column(sql.Integer)
    ticks_needed = sql.Column(sql.Float)
    ticks_left = sql.Column(sql.Float)

    def contents_weight(self):
        entities = Entity.query.filter(Entity.is_used_for(self)).all()
        return sum([entity.weight + entity.contents_weight() for entity in entities])

    def pyslatize(self, **overwrites):
        pyslatized = dict(entity_type=ENTITY_ACTIVITY, activity_id=self.id,
                          activity_name=self.name_tag, activity_params=self.name_params,
                          enclosing_entity=self.being_in.pyslatize())
        if self.has_property(P.DYNAMIC_NAMEABLE):
            pyslatized["dynamic_nameable"] = True
        return dict(pyslatized, **overwrites)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_ACTIVITY,
    }

    def __repr__(self):
        return "{{Activity name_tag={}, params={}, in={}, ticks={}/{}, req={}}}".format(self.name_tag, self.name_params,
                                                                                        self.being_in, self.ticks_left,
                                                                                        self.ticks_needed,
                                                                                        self.requirements)


class Combat(Entity):
    __tablename__ = "combats"

    def __init__(self):
        self.type = EntityType.by_name(Types.COMBAT)
        super().__init__()

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)
    recorded_violence = sql.Column(sqlalchemy_json_mutable.JsonDict, default=lambda: {})
    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey(EntityType.name), index=True)
    type = sql.orm.relationship(EntityType, uselist=False)

    def get_recorded_damage(self, fighter):
        if isinstance(fighter, Entity):
            fighter = fighter.id
        if isinstance(fighter, int):
            fighter = str(fighter)
        return self.recorded_violence.get(fighter, 0.0)

    def set_recorded_damage(self, fighter, value):
        if isinstance(fighter, Entity):
            fighter = fighter.id
        if isinstance(fighter, int):
            fighter = str(fighter)
        self.recorded_violence[fighter] = value

    def is_able_to_fight(self, fighter):
        from exeris.core import actions
        return fighter.has_property(P.COMBATABLE) and fighter.damage < 1.0 \
               and self.get_recorded_damage(fighter) <= actions.CombatProcess.DAMAGE_TO_DEFEAT

    def fighters_intents(self):
        return Intent.query.filter_by(type=main.Intents.COMBAT, target=self).all()

    def __repr__(self):
        return "{{Combat id={}}}".format(self.id)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_COMBAT,
    }


class GameDateCheckpoint(db.Model):
    __tablename__ = "game_date"

    id = sql.Column(sql.Integer, primary_key=True)
    game_date = sql.Column(sql.BigInteger, nullable=False)
    real_date = sql.Column(sql.BigInteger, nullable=False)


class EventTypeGroup(db.Model):
    __tablename__ = "event_type_groups"

    id = sql.Column(sql.Integer, primary_key=True)
    name = sql.Column(sql.String(32), index=True)


class SkillType(db.Model):
    __tablename__ = "skill_types"

    def __init__(self, name, general_name):
        self.name = name
        self.general_name = general_name

    name = sql.Column(sql.String(20), primary_key=True)
    general_name = sql.Column(sql.String(20), index=True)


class EventType(db.Model):
    __tablename__ = "event_types"

    IMPORTANT = 10
    NORMAL = 5
    LOW = 0

    name = sql.Column(sql.String, primary_key=True)
    severity = sql.Column(sql.SmallInteger)
    group_id = sql.Column(sql.Integer, sql.ForeignKey("event_type_groups.id"), nullable=True, index=True)
    group = sql.orm.relationship(EventTypeGroup, uselist=False)

    def __init__(self, name, severity=NORMAL, group=None):
        self.name = name
        self.severity = severity
        self.group = group


class Event(db.Model):
    __tablename__ = "events"

    id = sql.Column(sql.Integer, primary_key=True)
    type_name = sql.Column(sql.String, sql.ForeignKey("event_types.name"))
    type = sql.orm.relationship(EventType, uselist=False)
    params = sql.Column(sqlalchemy_json_mutable.JsonDict)
    date = sql.Column(sql.BigInteger)

    def __init__(self, event_type, params):
        if isinstance(event_type, str):
            event_type = EventType.query.get(event_type)
        self.type_name = event_type.name  # TODO MAKE SURE IT'S THE BEST WAY TO GO
        self.type = event_type

        self.params = params
        from exeris.core import general
        self.date = general.GameDate.now().game_timestamp

    @hybrid_property
    def observers(self):
        return [junction.observer for junction in self.observers_junction]

    def __repr__(self):
        return "{Event, type=" + self.type_name + ", params=" + str(self.params) + "}"


class EventObserver(db.Model):
    __tablename__ = "event_observers"

    observer_id = sql.Column(sql.Integer, sql.ForeignKey(Character.id), primary_key=True)
    observer = sql.orm.relationship(Character, uselist=False)
    event_id = sql.Column(sql.Integer, sql.ForeignKey(Event.id, ondelete='CASCADE'), primary_key=True)
    event = sql.orm.relationship(Event, uselist=False,
                                 backref=sql.orm.backref("observers_junction", cascade="all, delete-orphan",
                                                         passive_deletes=True))
    times_seen = sql.Column(sql.Integer)

    def __init__(self, event, observer):
        self.event = event
        self.observer = observer
        self.times_seen = 0

    def __repr__(self):
        return str(self.__class__) + str(self.__dict__)


class EntityContentsPreference(db.Model):
    __tablename__ = "entity_contents_preferences"

    character_id = sql.Column(sql.Integer, sql.ForeignKey(Character.id), primary_key=True)
    character = sql.orm.relationship(Character, uselist=False,
                                     primaryjoin="EntityContentsPreference.character_id == Character.id")

    def __init__(self, character, open_entity):
        self.character = character
        self.open_entity = open_entity

    open_entity_id = sql.Column(sql.Integer, sql.ForeignKey(Entity.id), primary_key=True)
    open_entity = sql.orm.relationship(Entity, uselist=False,
                                       primaryjoin="EntityContentsPreference.open_entity_id == Entity.id")


class EntityTypeProperty(db.Model):
    __tablename__ = "entity_type_properties"

    def __init__(self, name, data=None, type=None):
        self.type = type
        self.name = name
        self.data = data if data is not None else {}

    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey(EntityType.name), primary_key=True)
    type = sql.orm.relationship(EntityType, uselist=False, back_populates="properties")

    name = sql.Column(sql.String, primary_key=True)
    data = sql.Column(sqlalchemy_json_mutable.JsonDict)

    def __repr__(self):
        return "{{EntityTypeProperty name={}, for={}, data={}}}".format(self.name, self.type_name, self.data)


class EntityProperty(db.Model):
    __tablename__ = "entity_properties"

    entity_id = sql.Column(sql.Integer, sql.ForeignKey(Entity.id, ondelete="CASCADE"), primary_key=True)
    entity = sql.orm.relationship(Entity, uselist=False, back_populates="properties")

    def __init__(self, name, data=None, entity=None):
        self.entity = entity
        self.name = name
        self.data = data if data is not None else {}

    name = sql.Column(sql.String, primary_key=True)
    data = sql.Column(sqlalchemy_json_mutable.JsonDict)

    def __repr__(self):
        return "Property(entity: {}, name: {}, data {}".format(self.entity.id, self.name, self.data)


class PassageToNeighbour:
    """
    View class for displaying passage from the perspective of one side.
    It reflects changes on the decorated Passage instance.
    """

    def __init__(self, passage, other_side):
        self.passage = passage
        self._other_side = other_side

    @hybrid_property
    def other_side(self):
        return self._other_side

    @other_side.setter
    def other_side(self, value):
        self._other_side = value
        if self._other_side == self.passage.right_location:
            self.passage.right_location = value
        else:
            self.passage.left_location = value

    @hybrid_property
    def own_side(self):
        if self.passage.left_location == self._other_side:
            return self.passage.right_location
        return self.passage.left_location

    @own_side.setter
    def own_side(self, value):
        if self._other_side == self.passage.right_location:
            self.passage.left_location = value
        else:
            self.passage.right_location = value

    @staticmethod
    def get_other_side(passage, own_side):
        if passage.left_location == own_side:
            return passage.right_location
        elif passage.right_location == own_side:
            return passage.left_location

        raise ValueError("location {} is not on any side of passage {}", own_side, passage)

    def __repr__(self):
        return "{{PassageToNeighbour own={}, other={}, passage={}}}".format(
            self.own_side, self.other_side, self.passage)


class Location(Entity):
    __tablename__ = "locations"

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)

    def __init__(self, being_in, location_type, passage_type=None, weight=None, title=None):
        self.being_in = being_in
        self.weight = weight
        if not weight:
            self.weight = location_type.base_weight
        self.type = location_type

        if self.being_in is not None:
            db.session.add(Passage(self.being_in, self, passage_type))

        self.title = title
        super().__init__()

    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey(LocationType.name), index=True)
    type = sql.orm.relationship(LocationType, uselist=False)

    @hybrid_property
    def neighbours(self):
        neighbours = [passage.left_location for passage in self.right_passages]
        neighbours.extend([passage.right_location for passage in self.left_passages])
        return neighbours

    @hybrid_property
    def passages_to_neighbours(self):
        neighbours = [PassageToNeighbour(passage, passage.left_location) for passage in self.right_passages]
        neighbours.extend([PassageToNeighbour(passage, passage.right_location) for passage in self.left_passages])
        return neighbours

    @hybrid_property
    def quality(self):
        return 1.0

    def parent_locations(self):
        return [self]

    def characters_inside(self):
        return Character.query.filter(Character.is_in(self)) \
            .filter_by(type_name=Types.ALIVE_CHARACTER).all()

    def get_items_inside(self):
        return Item.query.filter(Item.is_in(self)).all()

    def remove(self):
        raise NotImplemented("remove is not yet implemented for Location")
        # TODO remove all passages to neighbours
        # make sure the path to RootLocation from every neighbour exists
        # or decide that all dependent locations will be destroyed
        # self.being_in = None

    def contents_weight(self):
        entities = Entity.query.filter(Entity.is_in(self)).all()
        return sum([entity.weight + entity.contents_weight() for entity in entities])

    def pyslatize(self, **overwrites):
        pyslatized = dict(entity_type=ENTITY_LOCATION, location_id=self.id,
                          location_name=self.type_name)
        if self.title:
            pyslatized["location_title"] = self.title
        prop = self.get_property(P.VISIBLE_MATERIAL)
        if prop:
            pyslatized["location_material"] = prop
        if self.has_property(P.DYNAMIC_NAMEABLE):
            pyslatized["dynamic_nameable"] = True
        domesticated_prop = self.get_property(P.DOMESTICATED)
        if domesticated_prop and "trusted" in domesticated_prop:
            pyslatized["trusted"] = domesticated_prop["trusted"]
        return dict(pyslatized, **overwrites)

    def __repr__(self):
        return "{{Location id={}, title={},type={}}}".format(self.id, self.title, self.type_name)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_LOCATION,
    }


class Sequences(db.Model):
    __tablename__ = "sequences"
    entity_union_sequence = sql.Sequence("entity_union_sequence")
    serial_id = sql.Column(sql.Integer, entity_union_sequence, primary_key=True)


class RootLocation(Location):
    __tablename__ = "root_locations"

    PERMANENT_MIN_DISTANCE = 3

    id = sql.Column(sql.Integer, sql.ForeignKey("locations.id"), primary_key=True)

    _position = sql.Column(gis.Geometry("POINT"), nullable=True, index=True)
    direction = sql.Column(sql.Integer)

    def __init__(self, position, direction):
        super().__init__(None, LocationType.by_name(Types.OUTSIDE), weight=0)
        self.position = position
        self.direction = direction

    @sql.orm.validates("direction")
    def validate_direction(self, key, direction):
        return direction % 360

    @hybrid_property
    def position(self):
        if self._position is None:
            return None
        return to_shape(self._position)

    @position.setter
    def position(self, position):  # we assume position is a Point
        x, y = position.x, position.y
        if not (0 <= x < MAP_WIDTH):
            x %= MAP_WIDTH
        if y < 0:
            y = -y
            x = (x + MAP_WIDTH / 2) % MAP_WIDTH
        if y > MAP_HEIGHT:
            y = MAP_HEIGHT - (y - MAP_HEIGHT)
            x = (x + MAP_WIDTH / 2) % MAP_WIDTH
        self._position = from_shape(Point(x, y))

    @position.expression
    def position(cls):
        return cls._position

    def is_permanent(self):
        fixed_items = Item.query.join(ItemType).filter(Item.is_in(self)).filter(~ItemType.portable).all()
        if fixed_items:
            return True

        # query for neighbouring locations using RISKY `being_in` check
        locations = Location.query.filter_by(parent_entity=self, role=Entity.ROLE_BEING_IN).all()

        if any([not loc.has_property(P.MOBILE) for loc in locations]):
            return True

        return False

    def can_be_permanent(self):
        other_root_locations = RootLocation.query. \
            filter(RootLocation.position.ST_DWithin(self.position.to_wkt(), RootLocation.PERMANENT_MIN_DISTANCE)).all()

        return all(not loc.is_permanent() for loc in other_root_locations if loc != self)

    def get_terrain_type(self):
        top_terrain = TerrainArea.query.filter(sql.func.ST_CoveredBy(from_shape(self.position), TerrainArea._terrain)). \
            order_by(TerrainArea.priority.desc()).first()
        if not top_terrain:
            return TerrainType.by_name(Types.SEA)
        return top_terrain.type

    def remove(self):
        if not self.is_empty():
            raise ValueError("trying to remove RootLocation (id: {}) which is not empty".format(self.id))
        db.session.delete(self)  # remove itself from the database

    def pyslatize(self, **overwrites):
        pyslatized = dict(entity_type=ENTITY_ROOT_LOCATION, location_id=self.id,
                          location_name=self.type_name, location_terrain=self.get_terrain_type().name)
        if self.has_property(P.DYNAMIC_NAMEABLE):
            pyslatized["dynamic_nameable"] = True
        return dict(pyslatized, **overwrites)

    def __repr__(self):
        return "{{RootLocation id={}, title={}, pos={}, direction={}, type={}}}".format(self.id, self.title,
                                                                                        self.position, self.direction,
                                                                                        self.type_name)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_ROOT_LOCATION,
    }


class BuriedContent(Entity):
    __tablename__ = "buried_contents"

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)

    _position = sql.Column(gis.Geometry("POINT"), nullable=True, index=True)

    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), index=True)
    type = sql.orm.relationship(EntityType, uselist=False)

    def __init__(self, position):
        self.position = position
        self.being_in = None
        self.weight = 0
        self.type = EntityType.by_name(Types.BURIED_HOLE)
        super().__init__()

    @hybrid_property
    def position(self):
        if self._position is None:
            return None
        return to_shape(self._position)

    @position.setter
    def position(self, position):  # we assume position is a Point
        x, y = position.x, position.y
        if not (0 <= x < MAP_WIDTH):
            x %= MAP_WIDTH
        if y < 0:
            y = -y
            x = (x + MAP_WIDTH / 2) % MAP_WIDTH
        if y > MAP_HEIGHT:
            y = MAP_HEIGHT - (y - MAP_HEIGHT)
            x = (x + MAP_WIDTH / 2) % MAP_WIDTH
        self._position = from_shape(Point(x, y))

    @position.expression
    def position(cls):
        return cls._position

    def remove(self):
        if not self.is_empty():
            raise ValueError("trying to remove BuriedContent (id: {}) which is not empty".format(self.id))
        db.session.delete(self)  # remove itself from the database

    def pyslatize(self, **overwrites):
        raise NotImplementedError("BuriedContent cannot be pyslatized")

    def __repr__(self):
        return "{{BuriedContent id={}, pos={}}}".format(self.id, self.position, self.type_name)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_BURIED_CONTENT,
    }


class TextContent(db.Model):
    __tablename__ = "text_contents"

    FORMAT_MD = "MD"
    FORMAT_HTML = "HTML"

    def __init__(self, entity, text_format=FORMAT_MD):
        self.entity = entity
        self.format = text_format

    entity_id = sql.Column(sql.Integer, sql.ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True)
    entity = sql.orm.relationship(Entity, uselist=False)

    title = sql.Column(sql.String)
    md_text = sql.Column(sql.String)
    html_text = sql.Column(sql.String)
    format = sql.Column(sql.String(4))


class PassageType(EntityType):
    __tablename__ = "passage_types"

    name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), primary_key=True)

    def __init__(self, name, unlimited):
        super().__init__(name)
        self.unlimited = unlimited

    unlimited = sql.Column(sql.Boolean)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_PASSAGE,
    }


class Passage(Entity):
    __tablename__ = "passages"

    def __init__(self, left_location, right_location, passage_type=None):
        self.weight = 0
        self.being_in = None
        self.left_location = left_location
        self.right_location = right_location
        if not passage_type:
            passage_type = EntityType.by_name(Types.DOOR)
        self.type = passage_type
        super().__init__()

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)

    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("passage_types.name"), index=True)
    type = sql.orm.relationship(PassageType, uselist=False)

    left_location_id = sql.Column(sql.Integer, sql.ForeignKey("locations.id"), index=True)
    right_location_id = sql.Column(sql.Integer, sql.ForeignKey("locations.id"), index=True)

    left_location = sql.orm.relationship(Location, primaryjoin=left_location_id == Location.id,
                                         backref="left_passages", uselist=False)
    right_location = sql.orm.relationship(Location, primaryjoin=right_location_id == Location.id,
                                          backref="right_passages", uselist=False)

    @hybrid_method
    def between(self, first_loc, second_loc):
        return (self.left_location == first_loc and self.right_location == second_loc) or \
               (self.right_location == first_loc and self.left_location == second_loc)

    @between.expression
    def between(self, first_loc, second_loc):
        return sql.or_((self.left_location == first_loc) & (self.right_location == second_loc),
                       (self.right_location == first_loc) & (self.left_location == second_loc))

    @hybrid_method
    def incident(self, loc):
        return self.left_location == loc or self.right_location == loc

    @incident.expression
    def incident(self, loc):
        return sql.or_((self.left_location == loc), (self.right_location == loc))

    def replace_location(self, location_to_replace, new_location):
        if self.left_location == location_to_replace:
            self.left_location = new_location
            if self.right_location.being_in == location_to_replace:
                self.right_location.being_in = new_location
            if location_to_replace.being_in == self.right_location:
                raise ValueError("Unable to update being_in for location {}".format(location_to_replace))
        elif self.right_location == location_to_replace:
            self.right_location = new_location
            if self.left_location.being_in == location_to_replace:
                self.left_location.being_in = new_location
            if location_to_replace.being_in == self.left_location:
                raise ValueError("Unable to update being_in for location {}".format(location_to_replace))
        else:
            ValueError("{} is not on either side of passage {}".format(location_to_replace, self))

    def is_accessible(self, only_through_unlimited=False):
        """
        Checks if the other side of the passage is accessible for any character.
        :return:
        """
        if only_through_unlimited:
            return self.type.unlimited
        return self.type.unlimited or self.is_open()

    def remove(self):
        self.left_location = None
        self.right_location = None
        db.session.delete(self)

    def is_open(self):
        return not self.has_property(P.CLOSEABLE, closed=True)

    def parent_locations(self):
        return [self.left_location, self.right_location]

    @hybrid_property
    def quality(self):
        return 1.0

    def pyslatize(self, **overwrites):
        pyslatized = dict(entity_type=ENTITY_PASSAGE, passage_id=self.id, passage_name=self.type_name)
        if self.has_property(P.CLOSEABLE):
            pyslatized["closed"] = not self.is_open()
        if self.has_property(P.INVISIBLE_PASSAGE):
            pyslatized["invisible"] = True
        if self.has_property(P.DYNAMIC_NAMEABLE):
            pyslatized["dynamic_nameable"] = True
        lock_prop = self.get_property(P.LOCKABLE)
        if lock_prop and lock_prop.get("lock_exists", False):
            pyslatized["unique_id"] = lock_prop["lock_id"]
        return dict(pyslatized, **overwrites)

    def __repr__(self):
        return "{{Passage id={}, type={}, left={}, right={}}}".format(self.id, self.type_name,
                                                                      self.left_location, self.right_location)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_PASSAGE,
    }


class UniqueIdentifier(db.Model):
    def __init__(self, value, entity_id, property_name):
        self.value = value
        self.entity_id = entity_id
        self.property_name = property_name

    value = sql.Column(sql.String, primary_key=True)
    entity_id = sql.Column(sql.Integer)
    property_name = sql.Column(sql.String)


class ObservedName(db.Model):
    __tablename__ = "observed_names"

    observer_id = sql.Column(sql.Integer, sql.ForeignKey("characters.id"), primary_key=True)
    observer = sql.orm.relationship(Character, uselist=False, foreign_keys=[observer_id])

    target_id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)
    target = sql.orm.relationship(Entity, uselist=False, foreign_keys=[target_id])

    name = sql.Column(sql.String)

    def __init__(self, observer, target, name):
        self.observer = observer
        self.target = target
        self.name = name

    def __repr__(self):
        return "{{ObservedName target={}, by={}, name={}}}".format(self.target, self.observer, self.name)


class Achievement(db.Model):
    __tablename__ = "achievements"

    def __init__(self, achiever, achievement):
        self.achiever = achiever
        self.achievement = achievement

    achiever_id = sql.Column(sql.String(PLAYER_ID_MAXLEN), sql.ForeignKey("players.id"), primary_key=True)
    achiever = sql.orm.relationship(Player, uselist=False, foreign_keys=[achiever_id])

    achievement = sql.Column(sql.String, primary_key=True)


class AchievementCharacterProgress(db.Model):
    __tablename__ = "achievement_character_progress"

    def __init__(self, name, character, details):
        self.name = name
        self.character = character
        self.details = details

    name = sql.Column(sql.String, primary_key=True)

    character_id = sql.Column(sql.Integer, sql.ForeignKey("characters.id"), primary_key=True)
    character = sql.orm.relationship(Character, uselist=False)

    details = sql.Column(psql.JSONB)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = sql.Column(sql.Integer, primary_key=True)

    def __init__(self, title_tag, title_params, text_tag, text_params, count=1, character=None, player=None):
        self.title_tag = title_tag
        self.title_params = title_params
        self.text_tag = text_tag
        self.text_params = text_params
        self.count = count
        self.character = character
        self.player = player

        from exeris.core import general
        self.game_date = general.GameDate.now().game_timestamp
        self.options = []

    player_id = sql.Column(sql.String, sql.ForeignKey("players.id"), nullable=True, index=True)
    player = sql.orm.relationship(Player, uselist=False)

    character_id = sql.Column(sql.Integer, sql.ForeignKey("characters.id"), nullable=True, index=True)
    character = sql.orm.relationship(Character, uselist=False)

    title_tag = sql.Column(sql.String)
    title_params = sql.Column(sqlalchemy_json_mutable.JsonDict, default=lambda: [])

    text_tag = sql.Column(sql.String)
    text_params = sql.Column(sqlalchemy_json_mutable.JsonDict, default=lambda: [])

    count = sql.Column(sql.Integer)
    icon_name = sql.Column(sql.String, default="undefined.png")
    options = sql.Column(sqlalchemy_json_mutable.JsonList, default=lambda: [])

    game_date = sql.Column(sql.BigInteger)

    def update_date(self):
        from exeris.core import general
        self.game_date = general.GameDate.now().game_timestamp

    def add_close_option(self):
        if not self.id:
            db.session.add(self)
            db.session.flush()  # to make sure that ID is available
        self.add_option("notification_close", {}, "notification.close", [self.id])

    def add_option(self, name_tag, name_params, endpoint, request_params):
        encoded_params_indexes = []
        for i in range(len(request_params)):
            param = request_params[i]
            if isinstance(param, Entity):
                request_params[i] = param.id
                encoded_params_indexes += [i]

        self.options.append({"name_tag": name_tag, "name_params": name_params,
                             "endpoint": endpoint, "request_params": request_params,
                             "encoded_indexes": encoded_params_indexes})

    def get_option(self, endpoint_name):
        for option in self.options:
            if option["endpoint"] == endpoint_name:
                return option
        return None

    @classmethod
    def by_id(cls, entity_id):
        return cls.query.get(entity_id)


class ScheduledTask(db.Model):
    __tablename__ = "scheduled_tasks"

    id = sql.Column(sql.Integer, primary_key=True)

    process_data = sql.Column(sqlalchemy_json_mutable.JsonList)
    execution_game_timestamp = sql.Column(sql.BigInteger, index=True)
    execution_interval = sql.Column(sql.Integer, nullable=True)

    def __init__(self, process_json, execution_game_timestamp, execution_interval=None):
        self.process_data = process_json
        self.execution_game_timestamp = execution_game_timestamp
        self.execution_interval = execution_interval

    def is_repeatable(self):
        return self.execution_interval is not None

    def stop_repeating(self):
        self.execution_interval = None


class EntityRecipe(db.Model):
    __tablename__ = "entity_recipes"

    id = sql.Column(sql.Integer, primary_key=True)

    def __init__(self, name_tag, name_params, requirements, ticks_needed, build_menu_category,
                 result=None, result_entity=None, activity_container=None):
        self.name_tag = name_tag
        self.name_params = name_params
        self.requirements = requirements
        self.ticks_needed = ticks_needed
        self.build_menu_category = build_menu_category
        self.result = result if result else []
        self.result_entity = result_entity
        self.activity_container = activity_container if activity_container else ["entity_specific_item"]

    name_tag = sql.Column(sql.String)
    name_params = sql.Column(sqlalchemy_json_mutable.JsonDict)

    requirements = sql.Column(sqlalchemy_json_mutable.JsonDict)
    ticks_needed = sql.Column(sql.Float)
    result = sql.Column(sqlalchemy_json_mutable.JsonList)  # a list of serialized Action constructors
    result_entity_id = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey(EntityType.name),
                                  nullable=True)  # EntityType being default result of the project
    result_entity = sql.orm.relationship(EntityType, uselist=False)
    activity_container = sql.Column(sqlalchemy_json_mutable.JsonList)

    build_menu_category_id = sql.Column(sql.Integer, sql.ForeignKey("build_menu_categories.id"), index=True)
    build_menu_category = sql.orm.relationship("BuildMenuCategory", uselist=False)


class BuildMenuCategory(db.Model):
    __tablename__ = "build_menu_categories"

    id = sql.Column(sql.Integer, primary_key=True)

    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent

    name = sql.Column(sql.String)
    parent_id = sql.Column(sql.Integer, sql.ForeignKey("build_menu_categories.id"), nullable=True, index=True)
    parent = sql.orm.relationship("BuildMenuCategory", primaryjoin=parent_id == id,
                                  foreign_keys=parent_id, remote_side=id, backref="child_categories", uselist=False)

    @classmethod
    def get_root_categories(cls):
        return cls.query.filter_by(parent=None).all()

    def get_recipes(self):
        return EntityRecipe.query.filter_by(build_menu_category=self).all()


# tables used by oauth2

class GrantToken(db.Model):
    __tablename__ = "grant_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(PLAYER_ID_MAXLEN), db.ForeignKey('players.id', ondelete='CASCADE'))
    user = db.relationship(Player)
    client_id = db.Column(db.String(40), nullable=False)
    code = db.Column(db.String(255), index=True, nullable=False)

    redirect_uri = db.Column(db.String(255))
    expires = db.Column(db.DateTime)

    _scopes = db.Column(db.Text)

    def delete(self):
        db.session.delete(self)
        db.session.commit()
        return self

    @property
    def scopes(self):
        if self._scopes:
            return self._scopes.split()
        return []


class BearerToken(db.Model):
    __tablename__ = "bearer_tokens"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(40), nullable=False)

    user_id = db.Column(db.String(PLAYER_ID_MAXLEN), db.ForeignKey('players.id'))
    user = db.relationship(Player)

    token_type = db.Column(db.String(40))

    access_token = db.Column(db.String(255), unique=True)
    refresh_token = db.Column(db.String(255), unique=True)
    expires = db.Column(db.DateTime)
    _scopes = db.Column(db.Text)

    def delete(self):
        db.session.delete(self)
        db.session.commit()
        return self

    @property
    def scopes(self):
        if self._scopes:
            return self._scopes.split()
        return []


class ResourceArea(db.Model):
    __tablename__ = "resource_areas"

    id = sql.Column(sql.Integer, primary_key=True)

    def __init__(self, resource_type, center, radius, efficiency, max_amount, amount=None):
        self.resource_type = resource_type
        self.center = center
        self.radius = radius
        self.efficiency = efficiency
        self.max_amount = max_amount
        self.amount = amount
        if amount is None:
            self.amount = self.max_amount

    @hybrid_method
    def in_area(self, position):
        return self.center.ST_DWithin(position.wkt, self.radius)

    @in_area.expression
    def in_area(self, position):
        return self.center.ST_DWithin(position.wkt, self.radius)

    resource_type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("item_types.name"))
    resource_type = sql.orm.relationship(ItemType, uselist=False)

    center = sql.Column(gis.Geometry("POINT"))  # TODO check if it's possible to have a precomp. index "center + radius"
    radius = sql.Column(sql.Float)

    @sql.orm.validates("center")
    def validate_center(self, key, center):
        return from_shape(center)

    efficiency = sql.Column(sql.Float)  # amount collected per unit of time
    amount = sql.Column(sql.Float)  # amount which can be collected before the area becomes exhausted
    max_amount = sql.Column(sql.Float)
    # TODO decide whether recovery rate should be set separately for every resource


class TerrainType(EntityType):
    __tablename__ = "terrain_types"

    name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("entity_types.name"), primary_key=True)

    def __init__(self, name, visibility=1.0, traversability=1.0):
        super().__init__(name)
        self.visibility = visibility
        self.traversability = traversability

    visibility = sql.Column(sql.Float)
    traversability = sql.Column(sql.Float)

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_TERRAIN_AREA,
    }


class TerrainArea(Entity):
    __tablename__ = "terrain_areas"

    def __init__(self, terrain_poly, terrain_type, priority=1):
        self.terrain = terrain_poly
        self.priority = priority
        self.type = terrain_type
        super().__init__()

    id = sql.Column(sql.Integer, sql.ForeignKey("entities.id"), primary_key=True)

    _terrain = sql.Column(gis.Geometry("POLYGON"), index=True)
    priority = sql.Column(sql.SmallInteger)
    type_name = sql.Column(sql.String(TYPE_NAME_MAXLEN), sql.ForeignKey("terrain_types.name"))
    type = sql.orm.relationship(TerrainType, uselist=False)

    @hybrid_property
    def terrain(self):
        return to_shape(self._terrain)

    @terrain.setter
    def terrain(self, value):
        self._terrain = from_shape(value)

    @terrain.expression
    def terrain(cls):
        return cls._terrain

    __mapper_args__ = {
        'polymorphic_identity': ENTITY_TERRAIN_AREA,
    }


class ResultantTerrainArea(db.Model):  # no overlays
    __tablename__ = "resultant_terrain_areas"

    id = sql.Column(sql.Integer, sql.ForeignKey("terrain_areas.id"), primary_key=True)

    def __init__(self, _terrain):
        self._terrain = _terrain

    _terrain = sql.Column(gis.Geometry("POLYGON"))

    @hybrid_property
    def terrain(self):
        return self._terrain

    @terrain.setter
    def terrain(self, value):
        self._terrain = from_shape(value)


AREA_KIND_VISIBILITY = 1
AREA_KIND_TRAVERSABILITY = 2


class PropertyArea(db.Model):
    """
    For example traversability or visibility
    """
    __tablename__ = "property_areas"

    id = sql.Column(sql.Integer, primary_key=True)

    def __init__(self, kind, value, priority, area, terrain_area=None):
        self.kind = kind
        self.value = value
        self.priority = priority
        self.area = area
        self.terrain_area = terrain_area

    terrain_area_id = sql.Column(sql.Integer, sql.ForeignKey("terrain_areas.id"), nullable=True)
    terrain_area = sql.orm.relationship(TerrainArea, uselist=False)

    kind = sql.Column(sql.SmallInteger, index=True)
    priority = sql.Column(sql.Integer, index=True)
    value = sql.Column(sql.Float)

    _area = sql.Column(gis.Geometry("POLYGON"))

    @hybrid_property
    def area(self):
        if self._area is None:
            return None
        return to_shape(self._area)

    @area.setter
    def area(self, value):
        if value:
            value = from_shape(value)
        self._area = value

    @area.expression
    def area(cls):
        return cls._area

    def __repr__(self):
        short_type_name = "trav" if self.kind == AREA_KIND_TRAVERSABILITY else "vis"
        return "{{PropertyArea {} prio={}, value={}, area={}}}".format(short_type_name, self.priority, self.value,
                                                                       self.area)


class ResultantPropertyArea:  # no overlays
    __tablename__ = "resultant_property_areas"

    id = sql.Column(sql.Integer)
    kind = sql.Column(sql.SmallInteger)
    value = sql.Column(sql.Float)

    _area = sql.Column(gis.Geometry("POLYGON"))

    @hybrid_property
    def area(self):
        return self._area

    @area.setter
    def area(self, value):
        self._area = from_shape(value)


def init_database_contents():
    event_types = [type_name for key_name, type_name in Events.__dict__.items() if not key_name.startswith("__")]

    for type_name in event_types:
        db.session.merge(EventType(type_name + "_doer"))
        db.session.merge(EventType(type_name + "_observer"))
        db.session.merge(EventType(type_name + "_target"))

    partial_events = [type_name for key_name, type_name in PartialEvents.__dict__.items()
                      if not key_name.startswith("__")]
    for type_name in partial_events:
        db.session.merge(EventType(type_name))

    if not PassageType.by_name(Types.DOOR):
        door_passage = PassageType(Types.DOOR, False)
        door_passage.properties.append(EntityTypeProperty(P.CLOSEABLE, {"closed": False}))
        invisible_passage = PassageType(Types.INVISIBLE_PASSAGE, True)
        invisible_passage.properties.append(EntityTypeProperty(P.INVISIBLE_PASSAGE))
        gangway_passage = PassageType(Types.GANGWAY, True)
        db.session.add_all([door_passage, invisible_passage, gangway_passage])
        alive_character = EntityType(Types.ALIVE_CHARACTER)
        alive_character.properties.append(EntityTypeProperty(P.LINE_OF_SIGHT, data={"base_range": 10}))
        alive_character.properties.append(EntityTypeProperty(P.STATES, data={
            main.States.TIREDNESS: {"initial": 0},
            main.States.SATIATION: {"initial": 0},
            main.States.HUNGER: {"initial": 0},
            main.States.STRENGTH: {"initial": 0.1},
            main.States.DURABILITY: {"initial": 0.1},
            main.States.FITNESS: {"initial": 0.1},
            main.States.PERCEPTION: {"initial": 0.1},
        }))
        alive_character.properties.append(
            EntityTypeProperty(P.MOBILE, data={
                "speed": 10,
                "traversable_terrains": [main.Types.LAND_TERRAIN]
            }))
        alive_character.properties.append(EntityTypeProperty(P.CONTROLLING_MOVEMENT))  # char can control own mobility
        alive_character.properties.append(EntityTypeProperty(P.WEAPONIZABLE, data={"attack": 5}))  # weaponless attack
        alive_character.properties.append(EntityTypeProperty(P.PREFERRED_EQUIPMENT, data={}))  # equipment settings
        alive_character.properties.append(EntityTypeProperty(P.COMBATABLE))
        db.session.add(alive_character)

        group_any_terrain = TypeGroup(Types.ANY_TERRAIN)
        group_land_terrain = TypeGroup(Types.LAND_TERRAIN)
        group_water_terrain = TypeGroup(Types.WATER_TERRAIN)
        group_any_terrain.add_to_group(group_land_terrain)
        group_any_terrain.add_to_group(group_water_terrain)
        db.session.add_all([group_any_terrain, group_land_terrain, group_water_terrain])

        dead_character = EntityType(Types.DEAD_CHARACTER)
        dead_character.properties.append(EntityTypeProperty(P.STATES, data={
            main.States.MODIFIERS: {"initial": {}},
        }))
        db.session.add(dead_character)

    db.session.merge(EntityType(Types.ACTIVITY))
    db.session.merge(EntityType(Types.BURIED_HOLE))
    db.session.merge(EntityType(Types.COMBAT))

    db.session.merge(TerrainType(Types.SEA))
    if not LocationType.by_name(Types.OUTSIDE):
        outside_type = LocationType(Types.OUTSIDE, 0)
        outside_type.properties.append(EntityTypeProperty(P.ENTERABLE))
        db.session.add(outside_type)

    db.session.flush()


def delete_all(seq):
    for element in seq:
        db.session.delete(element)


'''
# low-level functions to maintain ResultantTerrainArea as
@sql.event.listens_for(TerrainArea, "after_insert")
@sql.event.listens_for(TerrainArea, "after_update")
def receive_after_update(mapper, connection, target):

    terrain_envelope = db.session.query(TerrainArea._terrain.ST_Envelope().ST_AsText()).filter_by(id=target.id).first()
    to_be_deleted = ResultantTerrainArea.query.filter(ResultantTerrainArea._terrain.ST_Intersects(terrain_envelope)).all()
    delete_all(to_be_deleted)

    to_transfer = TerrainArea.query.filter(
            TerrainArea._terrain.ST_Intersects(terrain_envelope)
    ).order_by(TerrainArea.priority).all()

    db.session.add_all([ResultantTerrainArea(t._terrain) for t in to_transfer])
    # todo, should make sure these geometries are not intersecting with stuff with smaller priority

    print(ResultantTerrainArea.query.all())
'''
