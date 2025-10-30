# WordPress Integration for DbMoto XML Converter

This directory contains two ways to integrate the DbMoto XML converter API with your WordPress website:

## 🚀 Quick Integration (HTML Page)

Use `wordpress-integration.html` for a simple HTML page integration:

1. **Copy the HTML** from `wordpress-integration.html`
2. **Paste it** into a WordPress page using the "Custom HTML" block
3. **Update the API endpoint** in the JavaScript section:
   ```javascript
   const API_ENDPOINT = 'https://your-api-endpoint.execute-api.region.amazonaws.com/prod/convert';
   ```
4. **Publish the page**

## 🔧 WordPress Plugin (Recommended)

For a more professional and maintainable solution, use the WordPress plugin:

### Installation

1. **Zip the plugin folder:**
   ```bash
   cd wordpress-plugin
   zip -r dbmoto-converter.zip dbmoto-converter/
   ```

2. **Install via WordPress Admin:**
   - Go to `Plugins > Add New > Upload Plugin`
   - Upload `dbmoto-converter.zip`
   - Activate the plugin

3. **Configure API Endpoint:**
   Edit `dbmoto-converter.php` and update:
   ```php
   private $api_endpoint = 'https://your-api-endpoint.execute-api.region.amazonaws.com/prod/convert';
   ```

### Usage

#### Via Shortcode
Add the converter to any page or post:
```php
[dbmoto_converter]
```

#### Via PHP Code
```php
<?php echo do_shortcode('[dbmoto_converter]'); ?>
```

## 🚀 CI/CD Deployment

The WordPress plugin is **automatically deployed only when you create `dbmoto-*` version tags** that also trigger the Lambda deployment:

### Deployment Flow

```
Tag Push (dbmoto-*) → Lambda Deploy → WordPress Plugin Deploy
```

When you create a tag like `dbmoto-1.0.0`, the GitLab CI pipeline will:
1. ✅ Deploy the Lambda function to AWS (requires manual approval)
2. ✅ **Automatically deploy** the WordPress plugin via FTP (no manual approval needed)
3. 🔗 Plugin becomes available in WordPress admin

### Testing Deployment Locally

Before deploying via CI, test your setup locally:

```bash
cd dbmoto-converter/wordpress-plugin
./deploy-helper.sh
```

This script will:
- ✅ Validate plugin structure
- ✅ Check FTP connection
- ✅ Create deployment package
- ✅ Simulate deployment process

### Required GitLab CI Variables

Add these to your GitLab project CI/CD variables:

```bash
FTP_USER=your_ftp_username
FTP_PASSWORD=your_ftp_password
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
```

### Manual Plugin Installation

If you prefer manual installation:

1. **Download the plugin files** from `dbmoto-converter/wordpress-plugin/dbmoto-converter/`
2. **Zip the folder:**
   ```bash
   zip -r dbmoto-converter.zip dbmoto-converter/
   ```
3. **Upload via WordPress:**
   - Go to `Plugins > Add New > Upload Plugin`
   - Upload `dbmoto-converter.zip`
   - Activate the plugin

### Security Considerations

1. **File Validation:** The plugin validates XML file types and size
2. **Nonce Protection:** Uses WordPress nonces for security
3. **Error Handling:** Sanitizes all API responses
4. **Rate Limiting:** Consider adding rate limiting for production

### Customization

#### Change API Endpoint
```php
// In dbmoto-converter.php
private $api_endpoint = 'https://your-custom-endpoint.amazonaws.com/prod/convert';
```

#### Modify UI Text
```php
// In the render_converter method
<h2>Your Custom Title</h2>
<p>Your custom description...</p>
```

#### Add Custom Fields
```php
// Add to the form in render_converter method
<div class="form-group">
    <label for="custom-field">Custom Option:</label>
    <input type="text" id="custom-field" name="custom_field">
</div>
```

## 🐛 Troubleshooting

### Common Issues

1. **"File too large" error:**
   - Increase PHP upload limits in `php.ini`
   - Check WordPress upload size settings

2. **"API connection failed":**
   - Verify API endpoint URL is correct
   - Check CORS settings on API Gateway
   - Ensure API is deployed and accessible

3. **"Invalid file type":**
   - Ensure XML file has `.xml` extension
   - Check file MIME type is `text/xml` or `application/xml`

4. **Plugin not loading:**
   - Check WordPress error logs
   - Verify PHP version (requires 7.4+)
   - Ensure all plugin files are uploaded

### Debug Mode

Enable WordPress debug mode in `wp-config.php`:
```php
define('WP_DEBUG', true);
define('WP_DEBUG_LOG', true);
```

Check `/wp-content/debug.log` for errors.

## 📊 User Experience

1. **Upload:** User selects XML file (drag & drop supported)
2. **Configure:** Optional settings (include targets, etc.)
3. **Convert:** Progress bar shows upload/conversion status
4. **Download:** Direct download of ZIP file with all YAML files

## 🔒 Security Features

- ✅ File type validation
- ✅ Size limits (10MB)
- ✅ Nonce protection
- ✅ Input sanitization
- ✅ Error message sanitization
- ✅ CORS handling

## 🎨 Customization Examples

### Custom Styling
```css
/* Add to your theme's custom CSS */
#dbmoto-converter {
    border: 2px solid #007cba;
    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
}
```

### Custom JavaScript
```javascript
// Add to your theme's JS file
jQuery(document).on('dbmoto_conversion_complete', function(e, result) {
    console.log('Conversion completed:', result);
    // Add custom tracking/analytics here
});
```

## 📞 Support

For issues with the WordPress integration:

1. Check WordPress debug logs
2. Verify API endpoint connectivity
3. Test with the direct HTML version first
4. Check browser console for JavaScript errors

The HTML version (`wordpress-integration.html`) is useful for testing the API connection before implementing the full plugin.
