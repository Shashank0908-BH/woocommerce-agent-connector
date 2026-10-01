<?php
// Dev store only: there is no mail server, so drop outgoing mail silently.
add_filter( 'pre_wp_mail', '__return_true' );
