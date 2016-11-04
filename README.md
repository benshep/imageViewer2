# imageViewer2
The imageViewer2 program used on the ALICE accelerator.

This README information is gleaned from the page on the [ALICE wiki](http://projects.astec.ac.uk/ERLPManual/index.php/Python_Image_Capture) (4 Nov 2016).

**imageViewer2** is a program to capture images from screens around ALICE. It is written in Python and is the successor to the wildly popular MATLAB **imageViewer** program.

The frame grabber hardware has been upgraded to a USB box ([DFG/USB2pro from The Imaging Source](https://www.theimagingsource.com/products/converters/video-to-usb-2.0/dfgusb2pro/)) which is compatible with Windows 7. I decided to take the additional step of rewriting the image acquisition code in Python, and it seems to work quite nicely.

To use this software with the DFG/USB2pro box, you'll need the [IC Imaging Control C Library](https://www.theimagingsource.com/support/downloads-for-windows/software-development-kits-sdks/tisgrabberdll/). Copy the **tisgrabber_x64.dll** and **tisgrabber_x64.lib** files to the same folder as this repo. Clearly they're not included here for licensing reasons.

# Usage

![screenshot](https://github.com/benshep/imageViewer2/raw/master/ImageViewer2_screenshot.png)

By default, the program operates in 'auto grab' mode, constantly grabbing an image and displaying it in real time. Click the image to pause this and display a still image.

Use the list on the left to select a camera. Move it in or out by clicking the toggle switch at the top left of the window.

The **Options** button contains various options to change the behaviour of the program.

* **Automatically move screens in/out**. Moves the screen when you select a different one from the list. It won't move INJ-1 in (because it's slow) but it will move it out.
* **Show only frames with beam**. When ''not'' selected, it will capture at a blinding 25 Hz, just like the analogue display. With this option ticked, it compares the brightness of a frame to a given threshold (which can be adjusted - see below), and will only display the images which go above this threshold.
* **Fit Gaussians to profiles**. Performs a fit to the integrated horizontal and vertical profiles. The curve is shown on top of the profiles, and the fit parameters are displayed too (in mm, for screens where the calibration is known - which is all of them I think). The fitting is done by the LMFIT library and is blazing fast, so there's no real reason to turn it off.
* **Output data to EPICS**. Saves the fit data (positions and widths) in EPICS parameters corresponding to the displayed camera. The parameter names are (e.g.) <code>INJ-DIA-YAG-05:X</code>, <code>:Y</code>, <code>:W</code> and <code>:H</code> for the X position, Y position, width and height respectively. All values are in mm.
* **Process images in background**. Creates a new thread to do the fitting, rather than doing it serially with image capture. Actually doesn't seem to make much difference to speed.
* **Beam detection threshold**. Adjust this down if you don't see a beam (the image gets 'stuck'). Adjust it up if you see too many blank frames. It's a per-camera setting, and gets saved when you switch cameras. Click **Reset** to put the previous setting back. (For the curious, it's the difference-over-sum of the bright and dim fields.)
* **Image deinterlacing**. The cameras are interlaced, which means that each 768x572 image actually contains two 768x286 images, only one of which contains the beam. You can choose to ignore this, displaying and saving the images in the 'raw' state (**None**). The **Show brightest field** option copies the bright field to the dim field. The **Subtract dim field** option subtracts the dim field from the bright one, effectively removing the background. You won't ''quite'' see what's on the analogue screen, but it should be just the beam, with no artefacts.

In the **Tools** menu:

* **Save sequence of images** - saves a given number of frames to a subfolder of the work folder. The program displays the progress of this operation at the top right (under the **Save** button).
* **Record movie** - saves a sequence of frames into a movie. The filename is taken from the input box, with an MP4 extension instead of PNG. The movie codec is H264.

Train length is displayed at the top left. You can change it using the buttons on either side of the displayed train length, which increase or decrease it by approximately a factor of two (rounding to nice numbers) each time.

To save, hit the **Reset** button to set the filename automatically, followed by **Save**. Files are saved in the work folder. The last saved file is shown at the bottom right; click to open it in Windows.

You can display overlay images too; just select them using the relevant dropdown box. You can cycle through different images (for the screen you have selected) using the arrow buttons.

# Scripting

The program uses [ØMQ](http://zeromq.org/) to open itself up to scripting. You can write a script to select which camera is displayed and to move screens in and out. The script **socketClient.py** in the imageViewer2 folder shows how this is done. The code is very simple:

    import zmq
    
    # How to connect to the TCP server started by imageViewer2
    context = zmq.Context()
    
    # Control the selected screen by sending requests
    clientSocket = context.socket(zmq.REQ)
    clientSocket.connect('tcp://localhost:5559')
    clientSocket.send_string('INJ-2')
    msg = clientSocket.recv_string()
    print(msg) #should be 'INJ-2'

