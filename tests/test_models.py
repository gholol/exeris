from flask.ext.testing import TestCase
from geoalchemy2.shape import from_shape
from shapely.geometry import Point

from exeris.core.recipes import ActivityFactory
from exeris.core.main import db
from exeris.core.map import MAP_HEIGHT, MAP_WIDTH
from exeris.core.models import RootLocation, Location, Item, EntityProperty, EntityTypeProperty, \
    ItemType, Passage, EntityGroup, EntityGroupElement, EntityRecipe, BuildMenuCategory
from exeris.core import properties
from exeris.core.properties import EntityPropertyException, P
from tests import util


class LocationTest(TestCase):

    create_app = util.set_up_app_with_database

    def test_insert_basic(self):
        pos = Point(10, 20)
        root_loc = RootLocation(pos, False, 100)  # the simplest
        db.session.add(root_loc)

        root_loc2 = RootLocation(pos, False, 370)  # normalize direction
        self.assertEqual(10, root_loc2.direction)
        db.session.add(root_loc2)
        self.assertEqual(10, root_loc2.direction)

    def test_insert_validate_position(self):
        pos = Point(MAP_WIDTH + 20, 30)
        root_loc = RootLocation(pos, False, 30)  # normalize position
        db.session.add(root_loc)

        self.assertAlmostEqual(20, root_loc.position.x, places=6)
        self.assertAlmostEqual(30, root_loc.position.y, places=6)

        pos2 = Point(20, MAP_HEIGHT + 30)
        root_loc2 = RootLocation(pos2, False, 30)  # normalize position
        db.session.add(root_loc2)

        self.assertAlmostEqual(MAP_WIDTH / 2 + 20, root_loc2.position.x, places=6)
        self.assertAlmostEqual(MAP_HEIGHT - 30, root_loc2.position.y, places=6)

    def test_find_position(self):
        pos = Point(10, 20)
        root_loc = RootLocation(pos, False, 100)  # the simplest
        db.session.add(root_loc)

        good_query_results = db.session.query(RootLocation).filter_by(position=from_shape(Point(10, 20))).all()
        bad_query_results = db.session.query(RootLocation).filter_by(position=from_shape(Point(20, 20))).all()

        self.assertEqual(1, len(good_query_results))
        self.assertEqual(0, len(bad_query_results))

    def test_find_root(self):
        pos = Point(10, 20)
        root_loc = RootLocation(pos, False, 100)  # the simplest
        db.session.add(root_loc)

        farmyard = Location(root_loc, 0)
        db.session.add(farmyard)

        building = Location(farmyard, 0)
        db.session.add(building)

        room = Location(building, 0)
        db.session.add(room)

        self.assertEqual(root_loc, room.get_root())

    def test_methods__get_inside(self):

        root_loc = RootLocation(Point(20, 20), False, 100)
        loc = Location(root_loc, 100)

        db.session.add_all([root_loc, loc])

        # items
        type1 = ItemType("sword", 1000)
        db.session.add(type1)

        item1 = Item(type1, loc, 200)
        item2 = Item(type1, loc, 300)

        db.session.add_all([item1, item2])

        self.assertCountEqual([item1, item2], loc.get_items_inside())

        plr = util.create_player("Player1")

        # characters
        ch1 = util.create_character("Janusz", loc, plr)

        db.session.add(plr)
        db.session.add(ch1)

        self.assertCountEqual([ch1], loc.get_characters_inside())

    tearDown = util.tear_down_rollback


class EntityTest(TestCase):

    create_app = util.set_up_app_with_database

    def test_property_call_by_property(self):

        @properties.property_class
        class HappyPropertyType(properties.PropertyType):
            __property__ = "Happy"
            @properties.property_method
            def be_happy(self):
                pass

        item_type = ItemType("sickle", 500)

        item = Item(item_type, None, 100)
        prop = EntityProperty(entity=item, name="Happy", data={})
        db.session.add(prop)

        item.be_happy()  # item has property enabling the method, so it should be possible to call it

        item2 = Item(item_type, None, 200)
        type_prop = EntityTypeProperty(type=item_type, name="Happy", data={})
        db.session.add(type_prop)

        item2.be_happy()  # item type has property enabling the method, so it should be possible to call it

        db.session.delete(type_prop)

        self.assertRaises(EntityPropertyException, item2.be_happy)

    def test_has_property(self):

        @properties.property_class
        class SadPropertyType(properties.PropertyType):
            __property__ = "Sad"
            @properties.property_method
            def be_sad(self):
                pass

        item_type = ItemType("potato", 1, stackable=True)

        item = Item(item_type, None, 100)
        type_prop = EntityTypeProperty(item_type, "Sad", {"very": False, "cookies": 0})
        prop = EntityProperty(item, "Sad", {"very": True, "feel": "blue"})

        db.session.add_all([item_type, item, prop, type_prop])

        self.assertDictEqual({"very": True, "feel": "blue", "cookies": 0}, item.get_property("Sad"))

    tearDown = util.tear_down_rollback


class PassageTest(TestCase):

    create_app = util.set_up_app_with_database

    def test_accessibility(self):

        rt = RootLocation(Point(10, 20), False, 213)
        loc1 = Location(rt, 100)
        loc2 = Location(rt, 133)

        db.session.add_all([rt, loc1, loc2])
        passage1 = Passage.query.filter(Passage.between(rt, loc1)).first()
        passage2 = Passage.query.filter(Passage.between(rt, loc2)).first()

        open_window = EntityProperty(entity=passage1, name=P.WINDOW, data={"open": True})
        closed_window = EntityProperty(entity=passage2, name=P.WINDOW, data={"open": False})
        db.session.add_all([open_window, closed_window])

        self.assertTrue(passage1.is_accessible())
        self.assertFalse(passage2.is_accessible())

    tearDown = util.tear_down_rollback


class GroupTest(TestCase):

    create_app = util.set_up_app_with_database

    def test_group_direct_modification(self):

        self._setup_hammers()

        hammers = EntityGroup("group_hammers")

        hammers._children_junction.append(EntityGroupElement(self.stone_hammer))
        hammers._children_junction.append(EntityGroupElement(self.iron_hammer))
        hammers._children_junction.append(EntityGroupElement(self.marble_hammer))

        db.session.add(hammers)

        self.assertCountEqual([self.stone_hammer, self.iron_hammer, self.marble_hammer], hammers.children)

    def test_group_modification(self):

        self._setup_hammers()

        tools = EntityGroup("group_tools")
        hammers = EntityGroup("group_hammers")

        hammers.add_to_group(self.stone_hammer)
        hammers.add_to_group(self.iron_hammer)
        hammers.add_to_group(self.marble_hammer)

        tools.add_to_group(hammers)

        db.session.add_all([hammers, tools])

        self.assertCountEqual([self.stone_hammer, self.iron_hammer, self.marble_hammer], hammers.children)

        self.assertEqual([hammers], self.stone_hammer.parent_groups)
        self.assertEqual([tools], hammers.parent_groups)

    def test_containment(self):

        useful = EntityGroup("group_useful")
        tools = EntityGroup("group_tools")
        hammers = EntityGroup("group_hammers")
        cookies = EntityGroup("group_cookies")

        useful.add_to_group(tools)
        tools.add_to_group(hammers)

        db.session.add_all([useful, tools, hammers, cookies])

        self.assertTrue(tools.contains(hammers))
        self.assertTrue(useful.contains(hammers))
        self.assertFalse(tools.contains(cookies))
        self.assertFalse(hammers.contains(tools))

    def _setup_hammers(self):
        self.stone_hammer = ItemType("stone_hammer", 200)
        self.iron_hammer = ItemType("iron_hammer", 300)
        self.marble_hammer = ItemType("marble_hammer", 500)

        db.session.add_all([self.stone_hammer, self.iron_hammer, self.marble_hammer])

    def test_create_activity_from_recipe(self):

        stone_type = ItemType("stone", 50, stackable=True)
        hammer_type = ItemType("hammer", 100)

        tools_category = BuildMenuCategory("tools")
        rl = RootLocation(Point(1, 1), True, 32)
        db.session.add_all([rl, hammer_type, stone_type, tools_category])
        db.session.flush()

        recipe = EntityRecipe("project_manufacturing", {"item_name": "hammer"}, {"input": {stone_type.id: 20.0}}, 11,
                              tools_category, result_entity=hammer_type)

        db.session.add(recipe)

        factory = ActivityFactory()

        activity = factory.create_from_recipe(recipe, rl, 3)

        self.assertCountEqual({"input": {stone_type.id: 60.0}}, activity.requirements)
        self.assertEqual(33, activity.ticks_left)

    def test_build_menu_categories(self):

        buildings = BuildMenuCategory("buildings")
        wooden_buildings = BuildMenuCategory("wooden_buildings", buildings)
        stone_buildings = BuildMenuCategory("stone_buildings", buildings)
        tools = BuildMenuCategory("tools")
        steel_tools = BuildMenuCategory("steel_tools", tools)
        bone_tools = BuildMenuCategory("bone_tools", tools)

        db.session.add_all([buildings, wooden_buildings, stone_buildings, tools, steel_tools, bone_tools])

        steel_hammer = EntityRecipe("project_manu", {"name": "steel_hammer"}, {}, 11, steel_tools)
        steel_needle = EntityRecipe("project_manu", {"name": "steel_needle"}, {}, 5, steel_tools)
        stone_hut = EntityRecipe("project_build", {"name": "stone_hut"}, {}, 11, stone_buildings)

        db.session.add_all([steel_hammer, steel_needle, stone_hut])

        # buildings build menu structure
        self.assertCountEqual([stone_buildings, wooden_buildings], buildings.child_categories)
        self.assertCountEqual([stone_hut], stone_buildings.get_recipes())

        # tools build menu structure
        self.assertCountEqual([steel_tools, bone_tools], tools.child_categories)
        self.assertCountEqual([steel_hammer, steel_needle], steel_tools.get_recipes())



    tearDown = util.tear_down_rollback
