<?php
// Fictional catalogue, customers and orders. Deterministic (mt_srand) so the
// integration tests can assert on counts. Runs once; guarded by an option.
if ( get_option( 'connector_dev_seeded' ) ) { echo "already seeded\n"; return; }
mt_srand( 908 );

$teas = [ 'Masala Chai', 'Assam CTC', 'Darjeeling First Flush', 'Nilgiri Frost', 'Kashmiri Kahwa',
  'Tulsi Green', 'Lemongrass Ginger', 'Earl Grey', 'Cardamom Black', 'Saffron Kahwa',
  'Mint Green', 'Hibiscus Rose', 'Chamomile', 'Oolong Classic', 'Jasmine Pearl',
  'Rooibos Vanilla', 'Butterfly Pea', 'Moringa Green', 'Turmeric Latte Mix', 'Cinnamon Spice' ];
$products = [];
foreach ( $teas as $i => $name ) {
  $p = new WC_Product_Simple();
  $p->set_name( $name . ' 250g' );
  $p->set_sku( sprintf( 'TEA-%03d', $i + 1 ) );
  $p->set_regular_price( (string) ( 180 + 20 * ( $i % 9 ) ) );
  $p->set_manage_stock( true );
  $p->set_stock_quantity( 1000 ); // final levels are set after orders draw stock down
  $p->set_low_stock_amount( 5 );
  $p->set_short_description( 'Loose leaf, fictional demo product.' );
  $p->save();
  $products[] = $p;
}
// One variable product so variations are exercised.
$v = new WC_Product_Variable();
$v->set_name( 'Kulhad Mug' );
$v->set_sku( 'MUG-KULHAD' );
$attr = new WC_Product_Attribute();
$attr->set_name( 'Size' );
$attr->set_options( [ 'Small', 'Large' ] );
$attr->set_visible( true );
$attr->set_variation( true );
$v->set_attributes( [ $attr ] );
$v->save();
foreach ( [ 'Small' => [ 149, 3 ], 'Large' => [ 199, 40 ] ] as $size => [ $price, $qty ] ) {
  $var = new WC_Product_Variation();
  $var->set_parent_id( $v->get_id() );
  $var->set_attributes( [ 'size' => $size ] );
  $var->set_sku( 'MUG-KULHAD-' . strtoupper( $size ) );
  $var->set_regular_price( (string) $price );
  $var->set_manage_stock( true );
  $var->set_stock_quantity( $qty );
  $var->save();
}

$people = [ [ 'Aarav', 'Mehta' ], [ 'Diya', 'Iyer' ], [ 'Kabir', 'Nair' ], [ 'Ananya', 'Rao' ],
  [ 'Vihaan', 'Gupta' ], [ 'Ishita', 'Bose' ], [ 'Reyansh', 'Kulkarni' ], [ 'Meera', 'Pillai' ],
  [ 'Arjun', 'Saxena' ], [ 'Zoya', 'Khan' ], [ 'Rohan', 'Das' ], [ 'Tara', 'Menon' ] ];
$cities = [ [ 'Bengaluru', 'KA', '560001' ], [ 'Mumbai', 'MH', '400001' ], [ 'Pune', 'MH', '411001' ],
  [ 'Chennai', 'TN', '600001' ], [ 'Kolkata', 'WB', '700001' ], [ 'Delhi', 'DL', '110001' ] ];
$customer_ids = [];
foreach ( $people as $i => [ $first, $last ] ) {
  $c = new WC_Customer();
  $email = strtolower( $first . '.' . $last ) . '@example.test';
  $c->set_email( $email );
  $c->set_username( strtolower( $first . $last ) );
  $c->set_password( wp_generate_password( 24 ) );
  $c->set_first_name( $first );
  $c->set_last_name( $last );
  [ $city, $state, $pin ] = $cities[ $i % count( $cities ) ];
  foreach ( [ 'billing', 'shipping' ] as $type ) {
    $c->{"set_{$type}_first_name"}( $first );
    $c->{"set_{$type}_last_name"}( $last );
    $c->{"set_{$type}_address_1"}( ( 10 + $i ) . ' Demo Road' );
    $c->{"set_{$type}_city"}( $city );
    $c->{"set_{$type}_state"}( $state );
    $c->{"set_{$type}_postcode"}( $pin );
    $c->{"set_{$type}_country"}( 'IN' );
  }
  $c->set_billing_email( $email );
  $c->set_billing_phone( '+91-90000' . sprintf( '%05d', $i ) );
  $c->save();
  $customer_ids[] = $c->get_id();
}

$statuses = [ 'completed', 'completed', 'completed', 'processing', 'processing', 'on-hold',
  'pending', 'cancelled', 'refunded', 'failed' ];
$orders = (int) ( getenv( 'SEED_ORDERS' ) ?: 240 );
for ( $n = 0; $n < $orders; $n++ ) {
  $cid = $customer_ids[ $n % count( $customer_ids ) ];
  $cust = new WC_Customer( $cid );
  $order = wc_create_order( [ 'customer_id' => $cid ] );
  $lines = 1 + mt_rand( 0, 2 );
  for ( $l = 0; $l < $lines; $l++ ) {
    $order->add_product( $products[ mt_rand( 0, count( $products ) - 1 ) ], 1 + mt_rand( 0, 3 ) );
  }
  $order->set_address( $cust->get_billing(), 'billing' );
  $order->set_address( $cust->get_shipping(), 'shipping' );
  $order->set_payment_method( 'razorpay' );
  $order->set_payment_method_title( 'Razorpay (demo)' );
  $order->calculate_totals();
  // Spread over the last ~180 days so date filters mean something.
  $order->set_date_created( time() - mt_rand( 0, 180 * 86400 ) );
  $order->set_status( $statuses[ $n % count( $statuses ) ] );
  if ( $n % 25 === 0 ) {
    $order->add_order_note( 'Customer asked to change delivery slot (demo note).', 1 );
  }
  $order->save();
}
// Final stock levels: every seventh product out, low or near the threshold, the rest healthy.
foreach ( $products as $i => $p ) {
  $p = wc_get_product( $p->get_id() );
  $p->set_stock_quantity( $i % 7 < 3 ? [ 0, 2, 4 ][ $i % 7 ] : 20 + mt_rand( 0, 180 ) );
  $p->save();
}
update_option( 'connector_dev_seeded', 1 );
echo "seeded " . count( $products ) . " simple products, 1 variable product, "
  . count( $customer_ids ) . " customers, {$orders} orders\n";
