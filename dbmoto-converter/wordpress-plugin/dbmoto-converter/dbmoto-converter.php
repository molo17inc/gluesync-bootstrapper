<?php
/**
 * Plugin Name: DbMoto XML Converter
 * Plugin URI: https://dbmoto-converter.labs.molo17.com
 * Description: Convert DbMoto XML metadata files to Gluesync YAML configurations via AWS Lambda API. Supports files up to 50MB with automatic compression.
 * Version: 1.1.0
 * Author: Molo17
 * License: MIT
 */

// Prevent direct access
if (!defined('ABSPATH')) {
    exit;
}

class DbMotoConverterPlugin {

    private $api_endpoint = 'https://dbmoto-converter.labs.molo17.com/convert';

    public function __construct() {
        add_action('init', array($this, 'init'));
        add_shortcode('dbmoto_converter', array($this, 'render_converter'));
        add_action('wp_enqueue_scripts', array($this, 'enqueue_scripts'));
        add_action('wp_ajax_convert_dbmoto_xml', array($this, 'handle_ajax_conversion'));
        add_action('wp_ajax_nopriv_convert_dbmoto_xml', array($this, 'handle_ajax_conversion'));
    }

    public function init() {
        // Allow larger file uploads for XML files (up to 50MB, will be compressed)
        add_filter('upload_size_limit', function($size) {
            return 50 * 1024 * 1024; // 50MB
        });
    }

    public function enqueue_scripts() {
        wp_enqueue_script('dbmoto-converter-js', plugin_dir_url(__FILE__) . 'js/converter.js', array('jquery'), '1.0.0', true);
        wp_enqueue_style('dbmoto-converter-css', plugin_dir_url(__FILE__) . 'css/converter.css', array(), '1.0.0');

        wp_localize_script('dbmoto-converter-js', 'dbmoto_ajax', array(
            'ajax_url' => admin_url('admin-ajax.php'),
            'nonce' => wp_create_nonce('dbmoto_converter_nonce')
        ));
    }

    public function render_converter($atts) {
        ob_start();
        ?>
        <div id="dbmoto-converter">
            <h2>DBMoto XML to Gluesync YAML Converter</h2>
            <p>Upload your DBMoto metadata XML file to convert it into Gluesync YAML configuration files.</p>

            <form id="upload-form" enctype="multipart/form-data">
                <?php wp_nonce_field('dbmoto_converter_upload', 'dbmoto_converter_nonce'); ?>

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
                <h3>Conversion Complete!</h3>
                <div id="result-info"></div>
                <a id="download-btn" href="#" class="button button-secondary" style="display:none;" target="_blank">
                    Download Results (ZIP)
                </a>
            </div>

            <div id="error-container" style="display:none;">
                <div class="error-message"></div>
            </div>
        </div>
        <?php
        return ob_get_clean();
    }

    public function handle_ajax_conversion() {
        // Verify nonce
        if (!wp_verify_nonce($_POST['nonce'], 'dbmoto_converter_nonce')) {
            wp_send_json_error('Security check failed');
            return;
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
            
            // Automatically compress files larger than 1MB
            if ($file['size'] > 1 * 1024 * 1024) {
                $file_content = gzencode($file_content, 9); // Maximum compression
                $filename = $file['name'] . '.gz';
            }

            // Create multipart data for API call
            $boundary = wp_generate_password(24, false);
            $body = $this->build_multipart_body($boundary, $file_content, $filename, $include_targets);

            // Make API call
            $response = wp_remote_post($this->api_endpoint, array(
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
                wp_send_json_error('API returned error: ' . $response_code);
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

    private function build_multipart_body($boundary, $file_content, $filename, $include_targets) {
        $body = '';

        // Add file part
        $body .= '--' . $boundary . "\r\n";
        $body .= 'Content-Disposition: form-data; name="xml_file"; filename="' . $filename . '"' . "\r\n";
        $body .= 'Content-Type: text/xml' . "\r\n\r\n";
        $body .= $file_content . "\r\n";

        // Add include_targets part
        $body .= '--' . $boundary . "\r\n";
        $body .= 'Content-Disposition: form-data; name="include_targets"' . "\r\n\r\n";
        $body .= ($include_targets ? 'true' : 'false') . "\r\n";

        // End boundary
        $body .= '--' . $boundary . '--' . "\r\n";

        return $body;
    }
}

// Initialize plugin
new DbMotoConverterPlugin();
