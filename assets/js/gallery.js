// Wait until the page is fully loaded
document.addEventListener("DOMContentLoaded", function() {
  // Initialize Justified Gallery
  $('#myGallery').justifiedGallery({
    rowHeight : 150,
    lastRow : 'center',
    margins : 5
  });

  // Responsive adjustment (optional)
  $(window).resize(function() {
    $('[id^=gallery-]').justifiedGallery('norewind');
  });
});