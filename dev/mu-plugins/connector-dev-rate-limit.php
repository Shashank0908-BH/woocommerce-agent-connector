<?php
/**
 * Dev store only. WooCommerce core does not rate-limit /wc/v3, but the hosts
 * merchants run it on do (Cloudflare, WP Engine, Kinsta). This reproduces that:
 * a fixed window per API key, answered with 429 and Retry-After.
 *
 * Off by default. Turn on with:  wp option update connector_dev_rate_limit "10/2"
 * (10 requests per 2 seconds per key). Turn off with: wp option delete connector_dev_rate_limit
 */
add_filter( 'rest_pre_dispatch', function ( $result, $server, $request ) {
	$rule = get_option( 'connector_dev_rate_limit' );
	if ( ! $rule || 0 !== strpos( $request->get_route(), '/wc/' ) ) {
		return $result;
	}
	[ $limit, $window ] = array_map( 'intval', explode( '/', $rule ) + [ 1 => 1 ] );
	$who    = get_current_user_id() . ':' . ( $_SERVER['PHP_AUTH_USER'] ?? $_GET['oauth_consumer_key'] ?? 'anon' );
	$bucket = (int) floor( time() / max( 1, $window ) );
	$file   = sys_get_temp_dir() . '/connector-rl-' . md5( $who ) . '.json';
	$fh     = fopen( $file, 'c+' );
	flock( $fh, LOCK_EX );
	$state = json_decode( stream_get_contents( $fh ) ?: '{}', true ) ?: [];
	if ( ( $state['bucket'] ?? null ) !== $bucket ) {
		$state = [ 'bucket' => $bucket, 'count' => 0 ];
	}
	$state['count']++;
	ftruncate( $fh, 0 );
	rewind( $fh );
	fwrite( $fh, wp_json_encode( $state ) );
	flock( $fh, LOCK_UN );
	fclose( $fh );

	if ( $state['count'] > $limit ) {
		$retry = max( 1, ( $bucket + 1 ) * $window - time() );
		$response = new WP_REST_Response(
			[ 'code' => 'rate_limited', 'message' => 'Too many requests', 'data' => [ 'status' => 429 ] ],
			429
		);
		$response->header( 'Retry-After', (string) $retry );
		return $response;
	}
	return $result;
}, 10, 3 );
