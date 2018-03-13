// custom javascript

function slugify(text) {
    // https://gist.github.com/mathewbyrne/1280286
    return text.toString().toLowerCase()
      .replace(/\s+/g, '-')           // Replace spaces with -
      .replace(/[^\w\-]+/g, '')       // Remove all non-word chars
      .replace(/\-\-+/g, '-')         // Replace multiple - with single -
      .replace(/^-+/, '')             // Trim - from start of text
      .replace(/-+$/, '')             // Trim - from end of text
      .replace(/[\s_-]+/g, '-');
}


function applySlugify(source_selector, destination_selector) {
    $(destination_selector).keyup(function(){
        if (!$(destination_selector).hasClass("user-has-edited")){
            console.log('they touched');
            $(destination_selector).addClass("user-has-edited");
        }
    })
    $(source_selector).keyup(function(){
        if (!$(destination_selector).hasClass("user-has-edited")){
            $slug = slugify($(this).val());
            $(destination_selector).val($slug);
        }
    })
}

$(".incr-btn").on("click", function (e) {
    var $button = $(this);
    var oldValue = $button.parent().find('.quantity').val();
    $button.parent().find('.incr-btn[data-action="decrease"]').removeClass('inactive');
    if ($button.data('action') == "increase") {
        var newVal = parseFloat(oldValue) + 1;
    } else {
        // Don't allow decrementing below 0
        if (oldValue > 0) {
            var newVal = parseFloat(oldValue) - 1;
        } else {
            newVal = 0;
            $button.addClass('inactive');
        }
    }
    $button.parent().find('.quantity').val(newVal);
    e.preventDefault();
});
