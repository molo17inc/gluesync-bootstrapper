(function($) {
    'use strict';

    $(document).ready(function() {
        const form = $('#upload-form');
        const convertBtn = $('#convert-btn');
        const btnText = $('#btn-text');
        const btnLoading = $('#btn-loading');
        const progressContainer = $('#progress-container');
        const progressFill = $('#progress-fill');
        const progressText = $('#progress-text');
        const resultContainer = $('#result-container');
        const resultInfo = $('#result-info');
        const downloadBtn = $('#download-btn');
        const errorContainer = $('#error-container');
        const errorMessage = errorContainer.find('.error-message');

        form.on('submit', function(e) {
            e.preventDefault();

            const fileInput = $('#xml-file');
            const includeTargets = $('#include-targets').is(':checked');

            if (!fileInput[0].files[0]) {
                showError('Please select an XML file');
                return;
            }

            const file = fileInput[0].files[0];

            // Check file size (10MB limit)
            if (file.size > 10 * 1024 * 1024) {
                showError('File size must be less than 10MB');
                return;
            }

            // Show loading state
            setLoadingState(true);
            hideError();
            hideResult();

            // Create FormData for AJAX
            const formData = new FormData();
            formData.append('action', 'convert_dbmoto_xml');
            formData.append('nonce', dbmoto_ajax.nonce);
            formData.append('xml_file', file);
            formData.append('include_targets', includeTargets);

            // Show progress
            showProgress('Uploading file...');

            // Make AJAX request
            $.ajax({
                url: dbmoto_ajax.ajax_url,
                type: 'POST',
                data: formData,
                processData: false,
                contentType: false,
                xhr: function() {
                    const xhr = new window.XMLHttpRequest();
                    xhr.upload.addEventListener('progress', function(e) {
                        if (e.lengthComputable) {
                            const percentComplete = (e.loaded / e.total) * 50; // Upload is 50% of progress
                            updateProgress(percentComplete);
                            progressText.text('Uploading... ' + Math.round(percentComplete * 2) + '%');
                        }
                    });
                    return xhr;
                },
                success: function(response) {
                    if (response.success) {
                        updateProgress(75);
                        progressText.text('Processing...');

                        // Simulate some processing time
                        setTimeout(function() {
                            showSuccess(response.data);
                        }, 1000);
                    } else {
                        showError(response.data || 'Conversion failed');
                    }
                },
                error: function(xhr, status, error) {
                    console.error('AJAX Error:', xhr.responseText);
                    showError('Network error occurred. Please try again.');
                },
                complete: function() {
                    setLoadingState(false);
                    if (!resultContainer.is(':visible')) {
                        hideProgress();
                    }
                }
            });
        });

        function setLoadingState(loading) {
            convertBtn.prop('disabled', loading);
            btnText.toggle(!loading);
            btnLoading.toggle(loading);
        }

        function showProgress(text) {
            progressText.text(text);
            progressContainer.show();
            updateProgress(25);
        }

        function hideProgress() {
            progressContainer.hide();
            updateProgress(0);
        }

        function updateProgress(percent) {
            progressFill.css('width', percent + '%');
        }

        function showSuccess(result) {
            updateProgress(100);
            progressText.text('Complete!');

            resultInfo.html(`
                <p><strong>Files Generated:</strong> ${result.stats.yaml_files_generated}</p>
                <p><strong>Tables Processed:</strong> ${result.stats.tables_processed}</p>
                <p><strong>ZIP File Size:</strong> ${(result.stats.zip_file_size / 1024).toFixed(1)} KB</p>
            `);

            downloadBtn.attr('href', result.stats.zip_file_url);
            downloadBtn.show();

            setTimeout(function() {
                hideProgress();
                resultContainer.show();
            }, 500);
        }

        function hideResult() {
            resultContainer.hide();
            downloadBtn.hide();
        }

        function showError(message) {
            errorMessage.text(message);
            errorContainer.show();
        }

        function hideError() {
            errorContainer.hide();
        }
    });

})(jQuery);
