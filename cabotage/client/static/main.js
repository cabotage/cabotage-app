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
