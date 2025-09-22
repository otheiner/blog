// Wait until the page is fully loaded
document.addEventListener("DOMContentLoaded", function() {
  // Initialize Justified Gallery
  $('[id^=gallery-]').each(function() {
    $(this).justifiedGallery({
      rowHeight : 150,
      lastRow : 'justify',
      margins : 5
    });
  });

  // Responsive adjustment (optional)
  $(window).resize(function() {
    $('[id^=gallery-]').justifiedGallery('norewind');
  });
});