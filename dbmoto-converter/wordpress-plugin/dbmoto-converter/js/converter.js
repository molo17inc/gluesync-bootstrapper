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

        // Helper functions for UI state
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

        // Function to validate kit ID via AJAX
        async function validateKitId(kitId) {
            try {
                const response = await $.ajax({
                    url: dbmoto_ajax.ajaxurl,
                    type: 'POST',
                    data: {
                        action: 'validate_kit_id',
                        nonce: dbmoto_ajax.nonce,
                        kit_id: kitId
                    },
                    dataType: 'json'
                });
                
                if (response.success) {
                    return { valid: true };
                } else {
                    return { 
                        valid: false, 
                        message: response.data || 'Invalid kit ID. Please check and try again.'
                    };
                }
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

            // Basic validation
            if (!trialKitId) {
                showError('Please enter your kit ID');
                return;
            }

            // Validate kit ID before proceeding
            setLoading(true, 'Validating kit ID...');
            
            try {
                const validation = await validateKitId(trialKitId);
                
                if (!validation.valid) {
                    setLoading(false);
                    showError(validation.message);
                    return;
                }

                // If we get here, kit ID is valid - proceed with file upload
                const file = fileInput[0].files[0];
                
                // Check if file is selected
                if (!file) {
                    setLoading(false);
                    showError('Please select an XML file');
                    return;
                }


                // Check file size (50MB limit)
                if (file.size > 50 * 1024 * 1024) {
                    setLoading(false);
                    showError('File size must be less than 50MB');
                    return;
                }

                // Show loading state for upload
                setLoading(true, 'Uploading file...');
                
                // Proceed with file upload
                const formData = new FormData();
                formData.append('action', 'convert_dbmoto_xml');
                formData.append('nonce', dbmoto_ajax.nonce);
                formData.append('trial_kit_id', trialKitId);
                formData.append('include_targets', includeTargets ? 'true' : 'false');
                formData.append('xml_file', file);
                
                try {
                    const response = await $.ajax({
                        url: dbmoto_ajax.ajaxurl,
                        type: 'POST',
                        data: formData,
                        processData: false,
                        contentType: false,
                        xhr: function() {
                            const xhr = new window.XMLHttpRequest();
                            xhr.upload.addEventListener('progress', function(e) {
                                if (e.lengthComputable) {
                                    const percent = Math.round((e.loaded / e.total) * 100);
                                    progressFill.css('width', percent + '%');
                                    progressText.text('Uploading: ' + percent + '%');
                                }
                            }, false);
                            return xhr;
                        }
                    });
                    
                    if (response.success) {
                        // Handle successful upload
                        showResult('File processed successfully!');
                        if (response.data && response.data.download_url) {
                            downloadBtn.attr('href', response.data.download_url).show();
                        }
                    } else {
                        showError(response.data || 'Error processing file');
                    }
                } catch (error) {
                    console.error('Upload error:', error);
                    showError('Error uploading file. Please try again.');
                } finally {
                    setLoading(false);
                }
                
            } catch (error) {
                console.error('Error in form submission:', error);
                showError('An error occurred. Please try again.');
                setLoading(false);
            }
        }
        
        // Attach form submission handler
        form.on('submit', handleFormSubmission);

            try {
                // First validate the kit ID
                const validationResponse = await validateKitId(trialKitId);
                
                if (validationResponse.valid) {
                    // Kit ID is valid, proceed with file upload
                    uploadFile(file, trialKitId, includeTargets);
                } else {
                    // Invalid kit ID
                    showError(validationResponse.message || 'Invalid kit ID. Please check and try again.');
                    setLoadingState(false);
                }
            } catch (error) {
                console.error('Validation error:', error);
                showError('Error validating kit ID. Please try again or contact support if the problem persists.');
                setLoadingState(false);
            }
        });

        // Function to validate kit ID via AJAX
        function validateKitId(kitId) {
            return new Promise((resolve, reject) => {
                $.ajax({
                    url: dbmoto_ajax.ajax_url,
                    type: 'POST',
                    data: {
                        action: 'validate_kit_id',
                        kit_id: kitId,
                        nonce: dbmoto_ajax.nonce
                    },
                    dataType: 'json',
                    success: function(response) {
                        if (response.success) {
                            resolve(response.data);
                        } else {
                            reject(new Error(response.data || 'Validation failed'));
                        }
                    },
                    error: function(xhr, status, error) {
                        console.error('AJAX Error:', xhr.responseText);
                        reject(new Error(`AJAX error: ${status} - ${error}`));
                    }
                });
            });
        }

        // Function to handle file upload
        function uploadFile(file, trialKitId, includeTargets) {
            setLoadingState(true, 'Preparing upload...');

            const formData = new FormData();
            formData.append('action', 'convert_dbmoto_xml');
            formData.append('trial_kit_id', trialKitId);
            formData.append('include_targets', includeTargets ? '1' : '0');
            formData.append('xml_file', file);
            formData.append('nonce', dbmoto_ajax.nonce);

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
                    console.error('Upload error:', error);
                    let errorMessage = 'Error uploading file. ';
                    
                    try {
                        const response = JSON.parse(xhr.responseText);
                        if (response.data && response.data.message) {
                            errorMessage += response.data.message;
                        } else {
                            errorMessage += 'Please try again.';
                        }
                    } catch (e) {
                        errorMessage += 'Please try again.';
                    }
                    
                    showError(errorMessage);
                },
                complete: function() {
                    setLoadingState(false);
                    hideProgress();
                }
            });
        }

        function setLoadingState(loading, message = '') {
            if (loading) {
                convertBtn.prop('disabled', true);
                btnText.hide();
                btnLoading.show();
                if (message) {
                    progressText.text(message);
                }
            } else {
                convertBtn.prop('disabled', false);
                btnText.show();
                btnLoading.hide();
            }
        }

        function showProgress(text) {
            progressContainer.show();
            progressText.text(text);
            updateProgress(0);
        }

        function hideProgress() {
            progressContainer.hide();
            updateProgress(0);
        }

        function updateProgress(percent) {
            progressFill.css('width', percent + '%');
        }

        function showSuccess(result) {
            resultContainer.show();
            resultInfo.html('<p>Conversion successful! Your files are ready for download.</p>');
            
            if (result.download_url) {
                downloadBtn.attr('href', result.download_url).show();
            } else {
                downloadBtn.hide();
            }
            
            // Scroll to result
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
            
            // Scroll to error
            $('html, body').animate({
                scrollTop: errorContainer.offset().top - 100
            }, 500);
        }

        function hideError() {
            errorContainer.hide();
        }
    });

})(jQuery);
