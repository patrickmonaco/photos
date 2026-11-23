<?php
/**
 * Plugin Name: Photothèque – authentification REST
 * Description: Permet à la Photothèque de publier quand l'hébergeur supprime l'en-tête HTTP « Authorization » (PHP en CGI/FastCGI, fréquent chez OVH). Les identifiants arrivent dans l'en-tête X-Phototheque-Auth et sont vérifiés par WordPress comme d'habitude (mot de passe d'application). Diagnostic : /wp-json/phototheque/v1/diag
 * Version: 1.1
 *
 * Installation : copier ce fichier dans wp-content/mu-plugins/ (créer le dossier s'il n'existe pas).
 * Les extensions « must-use » sont chargées automatiquement, sans activation.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

define( 'PHOTOTHEQUE_AUTH_VERSION', '1.1' );

/** D'où viennent les identifiants reçus (pour le diagnostic) : 'php', 'authorization', 'fallback' ou ''. */
$GLOBALS['phototheque_auth_source'] = ! empty( $_SERVER['PHP_AUTH_USER'] ) ? 'php' : '';

// 1. En-tête Authorization arrivé sous un autre nom (réécriture Apache) : WordPress le lit déjà
//    dans la plupart des cas, on le complète par sécurité.
if ( empty( $_SERVER['PHP_AUTH_USER'] ) ) {
	foreach ( array( 'HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION', 'HTTP_X_PHOTOTHEQUE_AUTH' ) as $phototheque_key ) {
		if ( empty( $_SERVER[ $phototheque_key ] ) ) {
			continue;
		}
		$phototheque_value = trim( wp_unslash( $_SERVER[ $phototheque_key ] ) );
		if ( 0 !== stripos( $phototheque_value, 'basic ' ) ) {
			continue;
		}
		$phototheque_pair = base64_decode( substr( $phototheque_value, 6 ), true );
		if ( is_string( $phototheque_pair ) && false !== strpos( $phototheque_pair, ':' ) ) {
			list( $_SERVER['PHP_AUTH_USER'], $_SERVER['PHP_AUTH_PW'] ) = explode( ':', $phototheque_pair, 2 );
			$GLOBALS['phototheque_auth_source'] = 'HTTP_X_PHOTOTHEQUE_AUTH' === $phototheque_key ? 'fallback' : 'authorization';
			break;
		}
	}
	unset( $phototheque_key, $phototheque_value, $phototheque_pair );
}

// 2. Une extension ou le thème qui demande l'utilisateur courant trop tôt (avant que WordPress sache
//    qu'il s'agit d'un appel REST) fait ignorer le mot de passe d'application : on reconnaît l'appel
//    REST à son adresse.
add_filter(
	'application_password_is_api_request',
	function ( $is_api ) {
		if ( $is_api ) {
			return true;
		}
		$uri = isset( $_SERVER['REQUEST_URI'] ) ? (string) $_SERVER['REQUEST_URI'] : '';
		return false !== strpos( $uri, '/wp-json/' ) || isset( $_GET['rest_route'] );
	}
);

// 3. Diagnostic public, sans aucune donnée secrète : ce que le serveur a transmis à PHP.
add_action(
	'rest_api_init',
	function () {
		register_rest_route(
			'phototheque/v1',
			'/diag',
			array(
				'methods'             => 'GET',
				'permission_callback' => '__return_true',
				'callback'            => function () {
					global $wp_rest_application_password_status;
					$status = $wp_rest_application_password_status;
					return array(
						'plugin'               => PHOTOTHEQUE_AUTH_VERSION,
						'authorization_header' => ! empty( $_SERVER['HTTP_AUTHORIZATION'] ) || ! empty( $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ),
						'fallback_header'      => ! empty( $_SERVER['HTTP_X_PHOTOTHEQUE_AUTH'] ),
						'credentials_source'   => $GLOBALS['phototheque_auth_source'],
						'app_passwords'        => wp_is_application_passwords_available(),
						'user_id'              => get_current_user_id(),
						'password_check'       => is_wp_error( $status ) ? $status->get_error_code() : ( true === $status ? 'ok' : null ),
						'https'                => is_ssl(),
					);
				},
			)
		);
	}
);
