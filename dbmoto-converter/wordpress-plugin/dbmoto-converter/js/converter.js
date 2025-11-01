(function($) {
    'use strict';

    $(document).ready(function() {
        const form = $('#upload-form');
        if (!form.length) {
            return;
        }

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

        function setLoading(isLoading, message = '') {
            if (isLoading) {
                convertBtn.prop('disabled', true);
                btnText.text(message || 'Processing...');
                btnLoading.show();
            } else {
                convertBtn.prop('disabled', false);
                btnText.text('Convert');
                btnLoading.hide();
            }
        }

        function showProgress(text = '') {
            progressContainer.show();
            updateProgress(0, text);
        }

        function hideProgress() {
            progressContainer.hide();
            updateProgress(0, '');
        }

        function updateProgress(percent, text = null) {
            const safePercent = Math.max(0, Math.min(100, percent));
            progressFill.css('width', safePercent + '%');
            if (text !== null) {
                progressText.text(text);
            }
        }

        async function validateKitId(kitId) {
            try {
                const response = await $.ajax({
                    url: dbmoto_ajax.ajax_url,
                    type: 'POST',
                    data: {
                        action: 'validate_kit_id',
                        kit_id: kitId,
                        nonce: dbmoto_ajax.nonce
                    },
                    dataType: 'json'
                });

                if (response.success) {
                    return { valid: true };
                }

                return {
                    valid: false,
                    message: response.data || 'Invalid kit ID. Please check and try again.'
                };
            } catch (error) {
                console.error('Validation error:', error);
                return {
                    valid: false,
                    message: 'Error validating kit ID. Please try again.'
                };
            }
        }

        async function handleFormSubmission(e) {
            e.preventDefault();

            hideError();
            hideResult();

            const fileInput = $('#xml-file');
            const trialKitIdInput = $('#trial-kit-id');
            const includeTargets = $('#include-targets').is(':checked');
            const trialKitId = trialKitIdInput.val().trim();

            if (!trialKitId) {
                showError('Please enter your kit ID');
                return;
            }

            setLoading(true, 'Validating kit ID...');

            try {
                const validation = await validateKitId(trialKitId);

                if (!validation.valid) {
                    setLoading(false);
                    showError(validation.message);
                    return;
                }

                const file = fileInput[0]?.files?.[0];

                if (!file) {
                    setLoading(false);
                    showError('Please select an XML file');
                    return;
                }

                if (file.size > 50 * 1024 * 1024) {
                    setLoading(false);
                    showError('File size must be less than 50MB');
                    return;
                }

                setLoading(true, 'Uploading file...');
                showProgress('Uploading: 0%');

                const formData = new FormData();
                formData.append('action', 'convert_dbmoto_xml');
                formData.append('nonce', dbmoto_ajax.nonce);
                formData.append('trial_kit_id', trialKitId);
                formData.append('include_targets', includeTargets ? 'true' : 'false');
                formData.append('xml_file', file);

                try {
                    const response = await $.ajax({
                        url: dbmoto_ajax.ajax_url,
                        type: 'POST',
                        data: formData,
                        processData: false,
                        contentType: false,
                        xhr: function() {
                            const xhr = new window.XMLHttpRequest();
                            if (xhr.upload) {
                                xhr.upload.addEventListener('progress', function(event) {
                                    if (event.lengthComputable) {
                                        const percent = Math.round((event.loaded / event.total) * 100);
                                        updateProgress(percent, 'Uploading: ' + percent + '%');
                                    }
                                });
                            } else {
                                console.warn('Upload progress events are not supported in this browser.');
                            }
                            return xhr;
                        }
                    });

                    if (response.success) {
                        updateProgress(100, 'Processing complete');
                        showResult(response.data);
                    } else {
                        showError(response.data || 'Error processing file');
                    }
                } catch (error) {
                    console.error('Upload error:', error);
                    const message = error?.responseJSON?.data || 'Error uploading file. Please try again.';
                    showError(message);
                } finally {
                    setLoading(false);
                    hideProgress();
                }
            } catch (error) {
                console.error('Error in form submission:', error);
                showError('An error occurred. Please try again.');
                setLoading(false);
            }
        }

        function showResult(result) {
            resultContainer.show();
            const infoMessage = result?.message || 'Conversion completed successfully!';
            resultInfo.html('<p>' + infoMessage + '</p>');

            if (result?.download_url) {
                downloadBtn.attr('href', result.download_url).show();
            } else {
                downloadBtn.hide();
            }

            $('html, body').animate({
                scrollTop: resultContainer.offset().top - 100
            }, 500);
        }

        function hideResult() {
            resultContainer.hide();
            downloadBtn.hide();
        }

        function showError(message) {
            errorContainer.show();
            errorMessage.text(message);

            $('html, body').animate({
                scrollTop: errorContainer.offset().top - 100
            }, 500);
        }

        function hideError() {
            errorContainer.hide();
        }

        form.on('submit', handleFormSubmission);
    });

})(jQuery);
