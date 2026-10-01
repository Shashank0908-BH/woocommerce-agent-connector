<?php
// Writes REST API key pairs for the dev store in KEY=value form. Old dev keys
// are deleted first, so every run gives fresh keys.
global $wpdb;
$table = $wpdb->prefix . 'woocommerce_api_keys';
$wpdb->query( "DELETE FROM {$table} WHERE description LIKE 'connector-dev-%'" );
$admin = get_user_by( 'login', 'admin' );
foreach ( [ 'read' => 'WOO', 'read_write' => 'WOO_RW' ] as $perm => $prefix ) {
  $ck = 'ck_' . wc_rand_hash();
  $cs = 'cs_' . wc_rand_hash();
  $wpdb->insert( $table, [
    'user_id'         => $admin->ID,
    'description'     => 'connector-dev-' . $perm,
    'permissions'     => $perm,
    'consumer_key'    => wc_api_hash( $ck ),
    'consumer_secret' => $cs,
    'truncated_key'   => substr( $ck, -7 ),
  ] );
  echo "{$prefix}_CONSUMER_KEY={$ck}\n{$prefix}_CONSUMER_SECRET={$cs}\n";
}
