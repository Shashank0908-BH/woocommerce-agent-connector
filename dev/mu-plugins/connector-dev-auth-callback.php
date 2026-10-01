<?php
/**
 * Dev store only. WooCommerce delivers new API keys to the app's callback_url
 * with wp_safe_remote_post, which refuses private hosts, and the callback must
 * be https. In production the callback is a public https endpoint. Locally it
 * is the connector's listener on the host machine with a self-signed cert, so
 * allow exactly that host and skip certificate checks for it. Nothing else.
 */
const CONNECTOR_DEV_CALLBACK_HOST = 'host.docker.internal';

add_filter( 'http_request_host_is_external', function ( $external, $host ) {
	return $external || CONNECTOR_DEV_CALLBACK_HOST === $host;
}, 10, 2 );

add_filter( 'http_request_args', function ( $args, $url ) {
	if ( CONNECTOR_DEV_CALLBACK_HOST === wp_parse_url( $url, PHP_URL_HOST ) ) {
		$args['sslverify'] = false;
	}
	return $args;
}, 10, 2 );

// wp_safe_remote_post also only allows ports 80, 443 and 8080.
add_filter( 'http_allowed_safe_ports', function ( $ports, $host, $url ) {
	if ( CONNECTOR_DEV_CALLBACK_HOST === $host ) {
		$ports[] = (int) wp_parse_url( $url, PHP_URL_PORT );
	}
	return $ports;
}, 10, 3 );
