<?php
/**
 * Plugin Name: DbMoto XML Converter
 * Plugin URI: https://dbmoto-converter.labs.molo17.com
 * Description: Convert Syniti Replicate metadata XML files to Gluesync YAML configurations via AWS Lambda API. Supports files up to 50MB with automatic compression and trial kit validation.
 * Version: 1.2.0
 * Author: MOLO17
 * License: MIT
 */

// Prevent direct access
if (!defined('ABSPATH')) {
    exit;
}

class DbMotoConverterPlugin {

    private $api_base_url = 'https://dbmoto-converter.labs.molo17.com';
    private $api_convert_endpoint;
    private $trial_kits_base_url = 'https://molo17.com/gluesync-trials/';

    public function __construct() {
        $this->api_convert_endpoint = rtrim($this->api_base_url, '/') . '/convert';

        add_action('init', array($this, 'init'));
        add_shortcode('dbmoto_converter', array($this, 'render_converter'));
        add_action('wp_enqueue_scripts', array($this, 'enqueue_scripts'));
        
        // AJAX handlers
        add_action('wp_ajax_convert_dbmoto_xml', array($this, 'handle_ajax_conversion'));
        add_action('wp_ajax_nopriv_convert_dbmoto_xml', array($this, 'handle_ajax_conversion'));
        add_action('wp_ajax_validate_kit_id', array($this, 'handle_validate_kit_id'));
        add_action('wp_ajax_nopriv_validate_kit_id', array($this, 'handle_validate_kit_id'));
    }

    public function init() {
        // Allow larger file uploads for XML files (up to 50MB, will be compressed)
        add_filter('upload_size_limit', function($size) {
            return 50 * 1024 * 1024; // 50MB
        });
    }

    public function enqueue_scripts() {
        wp_enqueue_script('dbmoto-converter-js', plugin_dir_url(__FILE__) . 'js/converter.js', array('jquery'), '1.2.0', true);
        wp_enqueue_style('dbmoto-converter-css', plugin_dir_url(__FILE__) . 'css/converter.css', array(), '1.2.0');

        wp_localize_script('dbmoto-converter-js', 'dbmoto_ajax', array(
            'ajax_url' => admin_url('admin-ajax.php'),
            'nonce' => wp_create_nonce('dbmoto_converter_nonce')
        ));
    }

    public function render_converter($atts) {
        ob_start();
        ?>
        <div id="dbmoto-converter">
            <h2>Syniti Replicate's metadata XML to Gluesync's YAML Converter</h2>
            <p>Upload your Syniti Replicate metadata XML file to convert it into Gluesync YAML configuration files.</p>

            <form id="upload-form" enctype="multipart/form-data">
                <?php wp_nonce_field('dbmoto_converter_upload', 'dbmoto_converter_nonce'); ?>

                <div class="form-group">
                    <label for="trial-kit-id">Gluesync Kit ID:</label>
                    <input type="text" 
                           id="trial-kit-id" 
                           name="trial_kit_id" 
                           pattern="[a-f0-9]{32}" 
                           title="Please enter a valid 32-character Gluesync kit ID (e.g., abcdef1234567890abcdef1234567890)"
                           required>
                    <small>Enter your Gluesync kit identifier (32 hexadecimal characters) received from Gluesync kit download's page</small>
                </div>

                <div class="form-group">
                    <label for="xml-file">Select XML File:</label>
                    <input type="file" id="xml-file" name="xml_file" accept=".xml" required>
                    <small>Maximum file size: 50MB (files will be automatically compressed)</small>
                </div>

                <!-- <div class="form-group">
                    <label>
                        <input type="checkbox" id="include-targets" name="include_targets" checked>
                        Include target schemas
                    </label>
                </div> -->

                <button type="submit" id="convert-btn" class="button button-primary">
                    <span id="btn-text">Convert XML</span>
                    <span id="btn-loading" style="display:none;">Converting...</span>
                </button>
            </form>

            <div id="progress-container" style="display:none;">
                <div class="progress-bar">
                    <div class="progress-fill" id="progress-fill"></div>
                </div>
                <p id="progress-text">Processing your file...</p>
            </div>

            <div id="result-container" style="display:none;">
                <h3>Conversion completed</h3>
                <div id="result-info"></div>
                <a id="download-btn" href="#" class="button button-secondary" style="display:none;" target="_blank">
                    Download results (ZIP)
                </a>
            </div>

            <div id="error-container" style="display:none;">
                <div class="error-message"></div>
            </div>
        </div>
        <?php
        return ob_get_clean();
    }

    /**
     * Validate a trial kit ID
     * 
     * @param string $kit_id The kit ID to validate
     * @return array Array with 'valid' key and optional 'message' key
     */
    private function validate_trial_kit($kit_id) {
        $format_validation = $this->validate_trial_kit_format($kit_id);
        if (!$format_validation['valid']) {
            return $format_validation;
        }

        $file_validation = $this->validate_trial_kit_file_exists($kit_id);
        if (!$file_validation['valid']) {
            return $file_validation;
        }

        return array('valid' => true);
    }

    private function validate_trial_kit_format($kit_id) {
        if (empty($kit_id)) {
            return array('valid' => false, 'message' => 'Kit ID cannot be empty');
        }

        if (!preg_match('/^[a-f0-9]{32}$/i', $kit_id)) {
            return array(
                'valid' => false,
                'message' => 'Invalid kit ID format. Must be 32 hexadecimal characters (0-9, a-f).'
            );
        }

        return array('valid' => true);
    }

    private function validate_trial_kit_file_exists($kit_id) {
        $kit_id = strtolower($kit_id);
        $kit_url = $this->build_trial_kit_url($kit_id);

        $response = wp_remote_head($kit_url, array('timeout' => 15));

        if (is_wp_error($response)) {
            error_log('[DbMotoConverter] Kit validation HEAD request failed: ' . $response->get_error_message());
            return array(
                'valid' => false,
                'message' => 'Unable to validate kit ID right now. Please try again later.'
            );
        }

        $status_code = wp_remote_retrieve_response_code($response);

        if ($status_code === 200) {
            return array('valid' => true);
        }

        if ($status_code === 404) {
            return array(
                'valid' => false,
                'message' => 'Kit ID not found. Please check your kit identifier.'
            );
        }

        error_log(sprintf('[DbMotoConverter] Unexpected status (%d) when validating kit %s', $status_code, $kit_id));
        return array(
            'valid' => false,
            'message' => 'Unable to validate kit ID right now. Please try again later.'
        );
    }

    private function build_trial_kit_url($kit_id) {
        $kit_id = strtolower($kit_id);
        $filename = sprintf('%s-trial-kit.zip', $kit_id);

        return trailingslashit($this->trial_kits_base_url) . $filename;
    }

    public function handle_ajax_conversion() {
        // Verify nonce
        if (!isset($_POST['nonce']) || !wp_verify_nonce($_POST['nonce'], 'dbmoto_converter_nonce')) {
            wp_send_json_error('Security check failed');
            wp_die();
        }

        // Get and validate kit ID
        $trial_kit_id = isset($_POST['trial_kit_id']) ? sanitize_text_field($_POST['trial_kit_id']) : '';
        
        // Use our validation method
        $validation = $this->validate_trial_kit($trial_kit_id);
        if (!$validation['valid']) {
            wp_send_json_error($validation['message']);
            wp_die();
        }

        // Check if file was uploaded
        if (!isset($_FILES['xml_file']) || $_FILES['xml_file']['error'] !== UPLOAD_ERR_OK) {
            wp_send_json_error('No file uploaded or upload error');
            return;
        }

        $file = $_FILES['xml_file'];
        $include_targets = isset($_POST['include_targets']) && $_POST['include_targets'] === 'true';

        // Validate file type
        $allowed_types = array('text/xml', 'application/xml');
        if (!in_array($file['type'], $allowed_types) && !preg_match('/\.xml$/i', $file['name'])) {
            wp_send_json_error('Invalid file type. Please upload an XML file.');
            return;
        }

        // Check file size (50MB limit)
        if ($file['size'] > 50 * 1024 * 1024) {
            wp_send_json_error('File size must be less than 50MB');
            return;
        }

        try {
            // Prepare file for API call
            $file_content = file_get_contents($file['tmp_name']);
            $filename = $file['name'];
            
            // Debug: Check file content
            error_log("DEBUG: File content length: " . strlen($file_content));
            error_log("DEBUG: File tmp_name: " . $file['tmp_name']);
            error_log("DEBUG: File size: " . $file['size']);
            
            // Automatically compress files larger than 1MB
            $is_compressed = false;
            if ($file['size'] > 1 * 1024 * 1024) {
                $file_content = gzencode($file_content, 9); // Maximum compression
                $is_compressed = true;
                error_log("DEBUG: File compressed, new length: " . strlen($file_content));
            }

            // Create multipart data for API call
            $boundary = wp_generate_password(24, false);
            $body = $this->build_multipart_body($boundary, $file_content, $filename, $include_targets, $is_compressed);
            
            error_log("DEBUG: Multipart body length: " . strlen($body));
            error_log("DEBUG: Boundary: " . $boundary);

            // Make API call
            $response = wp_remote_post($this->api_convert_endpoint, array(
                'headers' => array(
                    'Content-Type' => 'multipart/form-data; boundary=' . $boundary,
                ),
                'body' => $body,
                'timeout' => 300, // 5 minutes timeout
            ));

            if (is_wp_error($response)) {
                wp_send_json_error('API request failed: ' . $response->get_error_message());
                return;
            }

            $response_code = wp_remote_retrieve_response_code($response);
            $response_body = wp_remote_retrieve_body($response);

            if ($response_code !== 200) {
                // Try to get detailed error message from API response
                $error_data = json_decode($response_body, true);
                $error_message = 'API returned error ' . $response_code;
                
                if ($error_data && isset($error_data['error'])) {
                    $error_message .= ': ' . $error_data['error'];
                } elseif ($error_data && isset($error_data['message'])) {
                    $error_message .= ': ' . $error_data['message'];
                } elseif (!empty($response_body)) {
                    $error_message .= ': ' . substr($response_body, 0, 200);
                }
                
                wp_send_json_error($error_message);
                return;
            }

            $result = json_decode($response_body, true);
            if (!$result) {
                wp_send_json_error('Invalid API response');
                return;
            }

            if ($result['status'] !== 'success') {
                wp_send_json_error($result['message'] ?? 'Conversion failed');
                return;
            }

            wp_send_json_success($result);

        } catch (Exception $e) {
            wp_send_json_error('Conversion error: ' . $e->getMessage());
        }
    }

    private function build_multipart_body($boundary, $file_content, $filename, $include_targets, $is_compressed) {
        // Build multipart body, sending compressed data as base64 to avoid binary corruption
        $body_parts = array();

        // If compressed, encode as base64
        if ($is_compressed) {
            $file_content = base64_encode($file_content);
            $filename = $filename . '.gz';
        }

        // Add file part
        $body_parts[] = '--' . $boundary . "\r\n";
        $body_parts[] = 'Content-Disposition: form-data; name="xml_file"; filename="' . $filename . '"' . "\r\n";
        $body_parts[] = 'Content-Type: text/xml' . "\r\n\r\n";
        $body_parts[] = $file_content;  // Now safe as base64 string or uncompressed text
        $body_parts[] = "\r\n";

        // Add include_targets part
        $body_parts[] = '--' . $boundary . "\r\n";
        $body_parts[] = 'Content-Disposition: form-data; name="include_targets"' . "\r\n\r\n";
        $body_parts[] = ($include_targets ? 'true' : 'false') . "\r\n";

        // End boundary
        $body_parts[] = '--' . $boundary . '--' . "\r\n";

        // Join as string (now safe)
        return implode('', $body_parts);
    }

    /**
     * Handle kit ID validation via AJAX
     */
    public function handle_validate_kit_id() {
        // Verify nonce
        if (!isset($_POST['nonce']) || !wp_verify_nonce($_POST['nonce'], 'dbmoto_converter_nonce')) {
            wp_send_json_error('Security check failed');
            return;
        }

        // Get and validate kit ID
        $kit_id = isset($_POST['kit_id']) ? sanitize_text_field($_POST['kit_id']) : '';
        
        if (empty($kit_id)) {
            wp_send_json_error('Kit ID is required');
            return;
        }

        // Validate kit ID format (32 hex characters)
        if (!preg_match('/^[a-f0-9]{32}$/i', $kit_id)) {
            wp_send_json_error('Invalid kit ID format. Must be 32 hexadecimal characters (0-9, a-f).');
            return;
        }

        $validation = $this->validate_trial_kit($kit_id);
        if (!$validation['valid']) {
            wp_send_json_error($validation['message']);
            return;
        }

        wp_send_json_success(array(
            'valid' => true,
            'message' => 'Kit ID is valid',
            'kit_id' => $kit_id,
            'download_url' => $this->build_trial_kit_url($kit_id)
        ));
    }

}

// Initialize plugin
new DbMotoConverterPlugin();
