FRAGMENTS.actions = (function() {

    $.subscribe("actions:update_actions_list", function() {
        socket.emit("update_actions_list", function(actions) {
            $.each(actions, function(idx, action) {
                var classes = "recipe btn btn-default";
                if (!action.enabled) {
                    classes += " disabled";
                }
                $("#actions_list > ol").append("<li class='" + classes + "' data-recipe='" + action.id + "'>" +
                    action.name + "</li>");
            });
        });
    });

    $(document).on("click", ".recipe", function(event) {
        var recipe = $(event.target);
        var recipe_id = recipe.data("recipe");
        socket.emit("activity_from_recipe_setup", recipe_id, function(rendered_code) {
            $("#recipe_setup_modal").remove();
            $(document.body).append(rendered_code);
            $("#recipe_setup_modal").modal();
        });
    });

    $(document).on("click", "#create_activity_from_recipe", function(event) {
        var recipe_id = $("#recipe_setup_modal").data("recipe");
        var user_input = {};
        $(".recipe_input").each(function() {
            user_input[$(this).prop("name")] = $(this).val();
        });

        var selected_entity_id = null;
        if ($(".selected_entity")) {
            selected_entity_id = $(".selected_entity").val();
        }

        socket.emit("create_activity_from_recipe", recipe_id, user_input, selected_entity_id, function() {
            $("#recipe_setup_modal").modal("hide");
        });
    });

    return {};
})();

$(function() {
    $.publish("actions:update_actions_list");
});